import torch
import torch.nn as nn
import torch.nn.functional as F
from esm.esmfold.v1.trunk import FoldingTrunk
from esm.esmfold.v1.esmfold import ESMFold
from openfold.utils.rigid_utils import Rigid
import utils
import numpy as np
import pickle
from collections import OrderedDict


class ProteinFeatures(nn.Module):
    def __init__(self, embedding_dim, num_rbf=16, augment_eps=0.):
        """ Extract protein features """
        super(ProteinFeatures, self).__init__()
        self.embedding_dim = embedding_dim
        self.augment_eps = augment_eps
        self.num_rbf = num_rbf
        self.edge_embedding = nn.Linear(num_rbf*25, embedding_dim, bias=False)

    def rbf(self, values, v_min=2., v_max=22.):
        """
        Returns RBF encodings in a new dimension at the end.
        """
        rbf_centers = torch.linspace(
            v_min, v_max, self.num_rbf, device=values.device)
        # view (*(1,)*len(values.shape),-1)
        rbf_centers = rbf_centers.view([1] * len(values.shape) + [-1])
        rbf_std = (v_max - v_min) / self.num_rbf
        z = (values.unsqueeze(-1) - rbf_centers) / rbf_std
        return torch.exp(-z ** 2)

    def forward(self, X, mask):
        """
        input  - 
        X    : [B, L, 4, 3]  N,CA,C,O,Virtual CB
        mask : [B, L]
        output -
        [B, L, L, embedding_dim]

        """
        if self.augment_eps > 0:
            X = X + self.augment_eps * torch.randn_like(X)

        b = X[:, :, 1, :] - X[:, :, 0, :]
        c = X[:, :, 2, :] - X[:, :, 1, :]
        a = torch.cross(b, c, dim=-1)
        Cb = -0.58273431*a + 0.56802827*b - 0.54067466*c + X[:, :, 1, :]
        Ca = X[:, :, 1, :]
        N = X[:, :, 0, :]
        C = X[:, :, 2, :]
        O = X[:, :, 3, :]
        atom_list = [N, Ca, C, Cb, O]  # [B, L, 3] for some specific atoms
        RBF_all = []  # [B, L]
        for atom1 in atom_list:
            for atom2 in atom_list:
                dist = torch.sqrt(torch.sum(torch.square(
                    atom1[:, :, None, :]-atom2[:, None, :, :])) + 1e-6)  # [B, L, L, 1]
                rbf_dist = self.rbf(dist)  # [B, L, L, 16]
                RBF_all.append(rbf_dist)
        RBF_all = torch.cat(tuple(RBF_all), dim=-1)  # [B, L, L, 16*25]
        # print(RBF_all.dtype)
        E = self.edge_embedding(RBF_all)
        mask_2d = mask[:, :, None] * mask[:, None, :]  # [B, L, L]
        mask_2d = mask_2d.unsqueeze(-1)
        return E * mask_2d


class esm_inpaint(nn.Module):
    def __init__(self, cfg, chunk_size=128, augment_eps=0.0, pattern="max"):
        """
        cfg is the defaulted input information to the esmfold
        """
        super().__init__()
        self.esmfold = ESMFold(cfg)
        self.cfg = cfg
        self.chunk_size = chunk_size
        self.esmfold.set_chunk_size(chunk_size)
        self.augment_eps = augment_eps
        self.ProteinFeatures = ProteinFeatures(cfg.trunk.pairwise_state_dim)
        self.seq_project = nn.Linear(cfg.trunk.sequence_state_dim, 20)
        # self.seq_project1 = nn.Linear(cfg.trunk.sequence_state_dim,128)
        # self.seq_project2 = nn.Linear(128,20)
        self.norm1 = nn.LayerNorm(cfg.trunk.sequence_state_dim)
        self.norm2 = nn.LayerNorm(128)

        self._froezen(patern=pattern)

    # def forward(self,coord,mask,S):
    def forward(self, coord, S, mask=None, recycle=1, bert_mask_structure=None):
        """
        coord : [B, L, 4, 3]_float32
        mask  : [B, L]_float32, 0 means padding mask and no loss, 1 means no padding mask and compute the loss
        S : [B, L]_long
        """
        if mask is None:
            mask = torch.ones_like(S).to(coord.device)
        # with torch.no_grad():
        #     output = self.esmfold(S,mask)
        # output_xyz = output['positions'][-1,...,:3,:]
        # output_plddt = output['plddt'][...,:3]

        # utils.output_to_pdb(output_xyz,S,plddt=output_plddt,file_path=file_path)

        # with open("result.pdb", "w") as f:
        #     f.write(output)

        # import biotite.structure.io as bsio
        # struct = bsio.load_structure("result.pdb", extra_fields=["b_factor"])
        # print(struct.b_factor.mean())  # this will be the pLDDT

        # Data augmentation by gaussian noise
        # if self.training and self.augment_eps > 0:
        #     coord = coord + self.augment_eps * torch.randn_like(coord)

        # add the distance embedding features
        if bert_mask_structure is not None:
            dis_embed = self.ProteinFeatures(coord, mask * bert_mask_structure)
        else:
            dis_embed = self.ProteinFeatures(coord, mask)

        # convert the coord to global frames
        bb_frame_atom = coord[:, :, 0:3, :]
        # # # rotation [B, L, 3, 3]
        # # # translation [B, L, 3]
        # # # bert_mask_structure [B, L] 0 unmask, 1 mask
        bb_rotation, bb_translation = utils.get_bb_frames(bb_frame_atom)
        # # bb_rotation[~bert_mask_structure] = torch.eye(3,device=coord.device) # black hole initialization
        # # bb_translation[~bert_mask_structure] = 0.0

        # # mask_frame = mask.reshape(*mask.shape,1,1)
        # # mask_frame = mask_frame.expand(*mask_frame.shape[:-2],4,4)
        bb_frame = torch.zeros(
            (*bb_rotation.shape[:-2], 4, 4), device=coord.device)
        bb_frame[..., :3, :3] = bb_rotation
        bb_frame[..., :3, 3] = bb_translation  # [B, L, 4, 4]
        bb_frame = Rigid.from_tensor_4x4(bb_frame)

        # running the esmfold
        # structure = self.esmfold(dis_embed,bb_frame,S,mask)
        # structure = self.esmfold(bb_frame,S,mask)
        structure = self.esmfold(dis_embed, S, mask, num_recycles=None)

        # refine the output
        # seq = self.seq_project1(self.norm1(structure['s_s']))
        # seq = self.seq_project2(self.norm2(seq))
        seq = self.seq_project(self.norm1(structure['s_s']))
        output_seq = F.log_softmax(seq, dim=-1)
        sample_seq = torch.multinomial(
            F.softmax(seq[0], dim=-1), num_samples=1).squeeze(-1)
        output_frams = structure['frames'][-1]

        initial_seq = sample_seq.unsqueeze(0)  # [B,L]
        # modify the initial stucture to the refine structure
        # for i in range(recycle):
        #     # do esmfold
        #     # output seq = esm2 sample seq, structure = esmfold predict structure
        #     # esmfold needs adjust
        #     pass

        output_xyz = structure['positions'][-1, ..., :3, :]
        output_ptm = structure['ptm']
        output_plddt = structure['plddt'][..., :3]
        output = {
            "log_softmax_aa": output_seq,
            "aatype": sample_seq.unsqueeze(0),
            "target_frames": bb_frame,
            "pred_frames": Rigid.from_tensor_7(output_frams),
            "positions": output_xyz,
            "ptm": output_ptm,
            "plddt": output_plddt,
            "s_s": structure['s_s'],
            "s_z": structure['s_z'],
        }
        return output

    @torch.no_grad()
    def infer(self, coord, S, T=1, motif_mask=None):
        """
        mask (float) represents the batch token mask [1, Length]
        motif_mask (float) represents the motif seqeunce and the scaffold seqeunce [1, Length]
        """
        mask = torch.ones_like(S).to(coord.device)
        dis_embed = self.ProteinFeatures(coord, mask * motif_mask)

        # stage1 : initialize the prior protein
        structure = self.esmfold(dis_embed, S, mask)
        seq = self.seq_project(self.norm1(structure['s_s']))
        output_seq = F.log_softmax(seq, dim=-1)
        prior_seq = torch.multinomial(
            F.softmax(seq[0], dim=-1), num_samples=1).squeeze(-1)
        prior_seq = prior_seq.unsqueeze(0)  # add the batch = 1 dimension
        prior_seq[motif_mask.to(bool)] = S[motif_mask.to(bool)]
        prior_frame = structure['frames'][-1]  # [8, B, L, 4, 4]

        # stage2 : sample condition on the prior protein and the motif sequence, structure T steps
        for i in range(T):

            # structure = self.esmfold(dis_embed=dis_embed, aa=prior_seq, mask=mask, motif_mask=motif_mask, prior_frame=Rigid.from_tensor_7(prior_frame),save_seq=True)
            structure = self.esmfold(dis_embed=dis_embed, aa=prior_seq, mask=mask,
                                     motif_mask=motif_mask, prior_frame=None, save_seq=True)
            prior_frame = structure['frames'][-1]
            prior_seq = structure['aatype']

        return self.esmfold.output_to_pdb(structure)
    
    @torch.no_grad()
    def mcmc(self,coord,S,T=1,motif_mask=None,beta=100,traj_output="./traj.txt"):
        mask = torch.ones_like(S).to(coord.device)
        dis_embed = self.ProteinFeatures(coord, mask * motif_mask)

        # stage1 : initialize the prior protein
        structure = self.esmfold(dis_embed, S, mask)
        print(structure['plddt'].shape)
        seq = self.seq_project(self.norm1(structure['s_s']))
        output_seq = F.log_softmax(seq, dim=-1)
        prior_seq = torch.multinomial(
            F.softmax(seq[0], dim=-1), num_samples=1).squeeze(-1)
        prior_seq = prior_seq.unsqueeze(0)  # add the batch = 1 dimension
        prior_seq[motif_mask.to(bool)] = S[motif_mask.to(bool)]
        structure_save = structure
        seq_save = prior_seq
        energy_prev = self.energy(structure)
        energy_list = [energy_prev]
        print(f"initial energy is {energy_prev}")
        # stage2 : sample condition on the prior protein and the motif sequence, structure T steps with MCMC
        for i in range(T+1):
            structure = self.esmfold(dis_embed=dis_embed, aa=seq_save, mask=mask,
                                     motif_mask=motif_mask, prior_frame=None, save_seq=True)
            prior_seq = structure['aatype']
            energy_current = self.energy(structure)
            energy_list.append(energy_current)
            flag = self.metroplis_hasting(energy_prev,energy_current,beta)
            if flag or i==0:
                seq_save = prior_seq
                structure_save = structure
                energy_prev = energy_current
            print(f"step {i} energy is {energy_current}, accept {flag}")
            if i % 1000 == 0 and i != 0 :
                beta = beta * 2 

        # writhe the energy list to a txt
        with open(traj_output,"w") as f:
            for entry in energy_list:
                f.write(str(entry) + "\n") 
        return self.esmfold.output_to_pdb(structure_save)

    @staticmethod
    def energy(output):
        output = {key: value.cpu() for key, value in output.items()}
        if 'mean_plddt' not in output.keys():
            plddt = torch.mean(output['plddt'][0,:,0]).item()
        else:
            plddt = output['mean_plddt'][0]
        pae,ptm = output['predicted_aligned_error'][0],output['ptm'][0]
        return (0.1 * torch.mean(pae) + 2 * (1- 0.5 * (plddt/100 + ptm))).item()

    @staticmethod
    def metroplis_hasting(energy_prev,energy_current,beta=10):
        """
        Metropolis-Hastings algorithm
        beta : 1/T
        """
        if energy_current < energy_prev:
            return True
        else:
            return np.exp(-(energy_current - energy_prev) * beta) > np.random.uniform()

    def _froezen(self, patern="min"):
        """
        Only training the following part of the modules:
        - ipa module
        - bb updata
        - transition
        - lm-head
        - ProteinFeatures
        """
        if patern == "max":
            for name, parameter in self.named_parameters():
                if name.startswith("esmfold.trunk.structure_module.ipa"):
                    parameter.requires_grad = True
                elif name.startswith("esmfold.trunk.structure_module.transition"):
                    parameter.requires_grad = True
                elif name.startswith("esmfold.trunk.structure_module.bb_update"):
                    parameter.requires_grad = True
                elif name.startswith("ProteinFeatures"):
                    parameter.requires_grad = True
                elif name.startswith("esmfold.lm_head"):
                    parameter.requires_grad = True
                elif name.startswith("seq_project"):
                    parameter.requires_grad = True
                else:
                    parameter.requires_grad = False
        elif patern == "min":
            for name, parameter in self.named_parameters():
                if name.startswith("ProteinFeatures"):
                    parameter.requires_grad = True
                elif name.startswith("esmfold.lm_head"):
                    parameter.requires_grad = True
                elif name.startswith("seq_project"):
                    parameter.requires_grad = True
                else:
                    parameter.requires_grad = False

    def inpaint_state_dict(self):
        """
        only the training parameters will be saved
        """
        inpaint_parameters = OrderedDict()
        for name, parameter in self.named_parameters():
            if parameter.requires_grad:
                inpaint_parameters[name] = parameter
        return inpaint_parameters

    def load_inpaint_dict(self, model):
        """
        load the inpaint parameter dictionary
        """
        if isinstance(model, str):
            model = torch.load(
                model, map_location=self.seq_project.weight.device)
            model = model['model_state_dict']
        self.load_state_dict(model, strict=False)
