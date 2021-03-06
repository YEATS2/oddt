from functools import partial

import numpy as np
from scipy.spatial.distance import cdist as distance
from scipy.sparse import vstack as sparse_vstack

from oddt.utils import is_molecule
from oddt.docking import autodock_vina
from oddt.docking.internal import vina_docking
from oddt.fingerprints import sparse_to_csr_matrix
import re

# ProDy
from prody import *
from pylab import *
ion()

# Biopython
from Bio.PDB import *

# QikProp
import csv
import subprocess
import os

# OpenBabel
import pybel

__all__ = ['close_contacts_descriptor',
           'fingerprints',
           'autodock_vina_descriptor',
           'oddt_vina_descriptor']


def atoms_by_type(atom_dict, types, mode='atomic_nums'):
    """Returns atom dictionaries based on given criteria.
    Currently we have 3 types of atom selection criteria:
        * atomic numbers ['atomic_nums']
        * Sybyl Atom Types ['atom_types_sybyl']
        * AutoDock4 atom types ['atom_types_ad4'] (http://autodock.scripps.edu/faqs-help/faq/where-do-i-set-the-autodock-4-force-field-parameters)

    Parameters
    ----------
    atom_dict: oddt.toolkit.Molecule.atom_dict
        Atom dictionary as implemeted in oddt.toolkit.Molecule class

    types: array-like
        List of atom types/numbers wanted.

    Returns
    -------
    out: dictionary of shape=[len(types)]
        A dictionary of queried atom types (types are keys of the dictionary).
        Values are of oddt.toolkit.Molecule.atom_dict type.
    """

    ad4_to_atomicnum = {
        'HD': 1, 'C': 6, 'CD': 6, 'A': 6, 'N': 7, 'NA': 7, 'OA': 8, 'F': 9,
        'MG': 12, 'P': 15, 'SA': 16, 'S': 16, 'CL': 17, 'CA': 20, 'MN': 25,
        'FE': 26, 'CU': 29, 'ZN': 30, 'BR': 35, 'I': 53
    }

    if mode == 'atomic_nums':
        return {num: atom_dict[atom_dict['atomicnum'] == num]
                for num in set(types)}
    elif mode == 'atom_types_sybyl':
        return {t: atom_dict[atom_dict['atomtype'] == t]
                for t in set(types)}
    elif mode == 'atom_types_ad4':
        # all AD4 atom types are capitalized
        types = [t.upper() for t in types]
        out = {}
        for t in set(types):
            if t in ad4_to_atomicnum:
                constraints = (atom_dict['atomicnum'] == ad4_to_atomicnum[t])
                # additoinal constraints for more specific atom types (donors,
                # acceptors, aromatic etc)
                if t == 'HD':
                    constraints &= atom_dict['isdonorh']
                elif t == 'C':
                    constraints &= ~atom_dict['isaromatic']
                elif t == 'CD':
                    # not canonical AD4 type, although used by NNscore, with no
                    # description
                    constraints &= ~atom_dict['isdonor']
                elif t == 'A':
                    constraints &= atom_dict['isaromatic']
                elif t in ('N', 'S'):
                    constraints &= ~atom_dict['isacceptor']
                elif t in ('NA', 'OA', 'SA'):
                    constraints &= atom_dict['isacceptor']

                out[t] = atom_dict[constraints]

            else:
                raise ValueError('Unsopported atom type: %s' % t)
    else:
        raise ValueError('Unsopported mode: %s' % mode)
    return out


class close_contacts_descriptor(object):
    def __init__(self,
                 protein=None,
                 cutoff=4,
                 mode='atomic_nums',
                 ligand_types=None,
                 protein_types=None,
                 aligned_pairs=False):
        """Close contacts descriptor which tallies atoms of type X in certain
        cutoff from atoms of type Y.

        Parameters
        ----------
        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        cutoff: int or list, shape=[n,] or shape=[n,2] (default=4)
            Cutoff for atoms in Angstroms given as an integer or a list of
            ranges, eg. [0, 4, 8, 12] or [[0,4],[4,8],[8,12]].
            Upper bound is always inclusive, lower exclusive.

        mode: string (default='atomic_nums')
            Method of atoms selection, as used in `atoms_by_type`

        ligand_types: array
            List of ligand atom types to use

        protein_types: array
            List of protein atom types to use

        aligned_pairs: bool (default=False)
            Flag indicating should permutation of types should be done,
            otherwise the atoms are treated as aligned pairs.
        """
        self.cutoff = np.atleast_1d(cutoff)
        # Cutoffs in fomr of continuous intervals (0,2,4,6,...)
        if len(self.cutoff) > 1 and self.cutoff.ndim == 1:
            self.cutoff = np.vstack((self.cutoff[:-1],
                                     self.cutoff[1:])).T
        elif self.cutoff.ndim > 2:
            raise ValueError('Unsupported shape of cutoff: %s' % self.cutoff.shape)

        # for pickle save original value
        self.original_cutoff = cutoff

        self.protein = protein
        self.ligand_types = ligand_types
        self.protein_types = protein_types if protein_types else ligand_types
        self.aligned_pairs = aligned_pairs
        self.mode = mode

        # setup titles
        if len(self.cutoff) == 1:
            self.titles = ['%s.%s' % (str(p), str(l))
                           for p in self.protein_types
                           for l in self.ligand_types
                           ]
        else:
            self.titles = ['%s.%s_%s-%s' % (str(p), str(l), str(c1), str(c2))
                           for p in self.protein_types
                           for l in self.ligand_types
                           for c1, c2 in self.cutoff
                           ]

    def build(self, ligands, protein=None):
        """Builds descriptors for series of ligands

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)
        return np.vstack(out)

    def build_normModes(self, ligands, protein, protein_pdb, nmbr_modes):
        """Builds normal modes eigenvalue descriptors for series of ligands
        and proteins.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        nmbr_modes: the number of normal modes that will be calculated.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        # New normal modes descriptors
        pdb = parsePDB(protein_pdb)
        calphas = pdb.select('calpha')

        anm = ANM('pdb ANM analysis')
        anm.buildHessian(calphas, cutoff=12.0)
        anm.getHessian().round(3)
        anm.calcModes(nmbr_modes)

        for mode in anm:
            desc = np.array(mode.getEigval(), dtype=int).flatten()
            out = [np.append(out[0], np.array(mode.getEigval()))]

        output = np.vstack(out)  
        return output


    def build_nmaLength(self, ligands, protein, protein_pdb, nmbr_modes):
        """Builds descriptors with number of eigenvalues
        and eigenvectors for series of ligands and proteins.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        nmbr_modes: the number of normal modes that will be calculated.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        # New normal modes descriptors
        # print(protein_pdb)
        pdb = parsePDB(protein_pdb)
        calphas = pdb.select('calpha')

        anm = ANM('pdb ANM analysis')
        anm.buildHessian(calphas, cutoff=12.0)
        anm.getHessian().round(3)
        anm.calcModes(n_modes = nmbr_modes)

        out = [np.append(out[0], np.array(len(anm.getEigvals())))]
        out = [np.append(out[0], np.array(len(anm.getEigvecs())))]

        output = np.vstack(out)  
        return output


    def build_bfactor(self, ligands, protein, protein_pdb):
        """Builds b-factor descriptors for series of ligands.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)


        parser = PDBParser()
        structure = parser.get_structure('pdb', protein_pdb)
        atoms = structure.get_atoms()

        # Get the b_factors for each atom the structure
        for a in atoms:
            out = [np.append(out[0], np.array(a.get_bfactor()))]

        return np.vstack(out)


    def build_qik(self, ligands, protein, ligand_sdf):
        """Builds qikprop properties descriptors for series of ligands.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference.

        ligand_sdf: the path to the sdf-file of the ligand.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        lig_id = ligand_sdf[-15:-4]
        subprocess.call("/opt/schrodinger2017-4/qikprop -NOJOBID %s" % ligand_sdf, shell=True)

        qikprops = {}
        with open("%s.CSV" % lig_id) as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                qikprops["FOSA"] = row["FOSA"]
                qikprops["FISA"] = row["FISA"]
                qikprops["WPSA"] = row["WPSA"]
                qikprops["QPlogPo/w"] = row["QPlogPo/w"]
                qikprops["QPlogHERG"] = row["QPlogHERG"]
                qikprops["QPlogKhsa"] = row["QPlogKhsa"]
                qikprops["QPPMDCK"] = row["QPPMDCK"]
                qikprops["QPlogKp"] = row["QPlogKp"]

        
        lig_name = lig_id[:-7]
        subprocess.call("rm " + lig_name + "*", shell=True)

        # Add QikProp properties as descriptors
        fail = 0
        for prop in qikprops:
            qikprops[prop]
            if qikprops[prop] == '':   # QikProp has failed
                if fail == 0:
                    with open("./qikFail.txt", "a+") as results:
                        results.write("%s \n" % lig_name)
                    fail = 1
                out = [np.append(out[0], np.array(0))]
            else:
                out = [np.append(out[0], np.array(float(qikprops[prop])))]

        return np.vstack(out)


    def build_eigval_qik(self, ligands, protein, protein_pdb, ligand_sdf):
        """Combines nma_eigenvalues and qikprop-properties descriptors for a series of ligands.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        ligand_sdf: the path to the sdf-file of the ligand.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        lig_id = ligand_sdf[-15:-4]
        subprocess.call("/opt/schrodinger2017-4/qikprop -NOJOBID %s" % ligand_sdf, shell=True)

        qikprops = {}
        with open("%s.CSV" % lig_id) as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                qikprops["FOSA"] = row["FOSA"]
                qikprops["FISA"] = row["FISA"]
                qikprops["WPSA"] = row["WPSA"]
                qikprops["QPlogPo/w"] = row["QPlogPo/w"]
                qikprops["QPlogHERG"] = row["QPlogHERG"]
                qikprops["QPlogKhsa"] = row["QPlogKhsa"]
                qikprops["QPPMDCK"] = row["QPPMDCK"]
                qikprops["QPlogKp"] = row["QPlogKp"]
        
        lig_name = lig_id[:-7]
        subprocess.call("rm " + lig_name + "*", shell=True)

        # Add QikProp properties as descriptors
        fail = 0
        for prop in qikprops:
            qikprops[prop]
            if qikprops[prop] == '':   # QikProp has failed
                if fail == 0:
                    with open("./qikFail_eigv.txt", "a+") as results:
                        results.write("%s \n" % lig_name)
                    fail = 1
                out = [np.append(out[0], np.array(0))]
            else:
                out = [np.append(out[0], np.array(float(qikprops[prop])))]

        # Add NMA Eigenvalues
        pdb = parsePDB(protein_pdb)
        calphas = pdb.select('calpha')

        anm = ANM('pdb ANM analysis')
        anm.buildHessian(calphas, cutoff=12.0)
        anm.getHessian().round(3)
        anm.calcModes()

        for mode in anm:
            desc = np.array(mode.getEigval(), dtype=int).flatten()
            out = [np.append(out[0], np.array(mode.getEigval()))]

        return np.vstack(out)


    def build_num_rots(self, ligands, protein, ligand_sdf):
        """Builds number of rotatable bonds descriptors for a series of ligands.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        ligand_sdf: the path to the sdf-file of the ligand.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        for mol in pybel.readfile("sdf", ligand_sdf): # can be sdf or mol2
            out = [np.append(out[0], np.array(mol.OBMol.NumRotors()))]

        output = np.vstack(out)  
        return output


    def build_num_aromat_rings(self, ligands, protein, ligand_sdf):
        """Builds number of aromatic rings descriptors for series of ligands.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        ligand_sdf: the path to the sdf-file of the ligand.
        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        for mol in pybel.readfile("sdf", ligand_sdf): # could be sdf or mol2
            result = ["Aromatic" for r in mol.OBMol.GetSSSR() if r.IsAromatic()]
            out = [np.append(out[0], np.array(len(result)))]

        output = np.vstack(out)  
        return output


    def build_improved(self, ligands, protein, protein_pdb, ligand_sdf, nmbr_modes):
        """Combines qikprop properties, #aromatic rings, #rotatable bons, #eigenvectors
        and #eigenvalues as descriptors. Used in ET-Score.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        ligand_sdf: the path to the sdf-file of the ligand.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        lig_id = ligand_sdf[-15:-4]
        subprocess.call("/opt/schrodinger2017-4/qikprop -NOJOBID %s" % ligand_sdf, shell=True)

        qikprops = {}
        with open("%s.CSV" % lig_id) as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                qikprops["FOSA"] = row["FOSA"]
                qikprops["FISA"] = row["FISA"]
                qikprops["WPSA"] = row["WPSA"]
                qikprops["QPlogPo/w"] = row["QPlogPo/w"]
                qikprops["QPlogHERG"] = row["QPlogHERG"]
                qikprops["QPlogKhsa"] = row["QPlogKhsa"]
                qikprops["QPPMDCK"] = row["QPPMDCK"]
                qikprops["QPlogKp"] = row["QPlogKp"]
        
        lig_name = lig_id[:-7]
        subprocess.call("rm " + lig_name + "*", shell=True)

        # Add QikProp properties as descriptors
        fail = 0
        for prop in qikprops:
            qikprops[prop]
            if qikprops[prop] == '':   # QikProp has failed
                if fail == 0:
                    with open("./qikFail_eigv.txt", "a+") as results:
                        results.write("%s \n" % lig_name)
                    fail = 1
                out = [np.append(out[0], np.array(0))]
            else:
                out = [np.append(out[0], np.array(float(qikprops[prop])))]

        # Add Number of rotatable bonds
        for mol in pybel.readfile("sdf", ligand_sdf): # can be sdf or mol2
            out = [np.append(out[0], np.array(mol.OBMol.NumRotors()))]

        # Add number of aromatic rings
        for mol in pybel.readfile("sdf", ligand_sdf): # could be sdf or mol2
            result = ["Aromatic" for r in mol.OBMol.GetSSSR() if r.IsAromatic()]
            out = [np.append(out[0], np.array(len(result)))]

        pdb = parsePDB(protein_pdb)
        calphas = pdb.select('calpha')

        anm = ANM('pdb ANM analysis')
        anm.buildHessian(calphas, cutoff=12.0)
        anm.getHessian().round(3)
        anm.calcModes(n_modes = nmbr_modes)

        # Add NMA Length
        out = [np.append(out[0], np.array(len(anm.getEigvals())))]
        out = [np.append(out[0], np.array(len(anm.getEigvecs())))]

        # Add NMA Eigenvalues
        # for mode in anm:
        #     desc = np.array(mode.getEigval(), dtype=int).flatten()
        #     out = [np.append(out[0], np.array(mode.getEigval()))]

        return np.vstack(out)


    def build_stand_alone(self, ligands, lig_sdf, protein, prot_pdb, nmbr_modes, schroedinger_path):
        """Combines qikprop properties, #aromatic rings, #rotatable bons, #eigenvectors
        and #eigenvalues as descriptors. Used in ET-Score.

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        protein_pdb: the pdb id of the protein.

        ligand_sdf: the path to the sdf-file of the ligand.

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            mol_dict = atoms_by_type(mol.atom_dict, self.ligand_types, self.mode)
            if self.aligned_pairs:
                pairs = zip(self.ligand_types, self.protein_types)
            else:
                pairs = [(mol_type, prot_type)
                         for mol_type in self.ligand_types
                         for prot_type in self.protein_types]

            dist = distance(self.protein.atom_dict['coords'],
                            mol.atom_dict['coords'])
            within_cutoff = (dist <= self.cutoff.max()).any(axis=1)
            local_protein_dict = self.protein.atom_dict[within_cutoff]

            prot_dict = atoms_by_type(local_protein_dict, self.protein_types,
                                      self.mode)
            desc = []
            for mol_type, prot_type in pairs:
                d = distance(prot_dict[prot_type]['coords'],
                             mol_dict[mol_type]['coords'])[..., np.newaxis]
                if len(self.cutoff) > 1:
                    count = ((d > self.cutoff[..., 0]) &
                             (d <= self.cutoff[..., 1])).sum(axis=(0, 1))

                else:
                    count = (d <= self.cutoff).sum()
                desc.append(count)
            desc = np.array(desc, dtype=int).flatten()
            out.append(desc)

        if schroedinger_path[-1] != "/":
            schroedinger_path += "/"

        subprocess.call(schroedinger_path + "qikprop -NOJOBID %s" % lig_sdf, shell=True)
        if ("/" in lig_sdf):
            pattern = re.compile(r'/([^/]+)\.sdf')
            lig_name =  re.search(pattern, lig_sdf).group(1)
            print(lig_name)
        else:
            lig_name = lig_sdf[:-4]  # remove .sdf

        qikprops = {}
        with open("%s.CSV" % lig_name) as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                qikprops["FOSA"] = row["FOSA"]
                qikprops["FISA"] = row["FISA"]
                qikprops["WPSA"] = row["WPSA"]
                qikprops["QPlogPo/w"] = row["QPlogPo/w"]
                qikprops["QPlogHERG"] = row["QPlogHERG"]
                qikprops["QPlogKhsa"] = row["QPlogKhsa"]
                qikprops["QPPMDCK"] = row["QPPMDCK"]
                qikprops["QPlogKp"] = row["QPlogKp"]
        

        # Add QikProp properties as descriptors
        fail = 0
        for prop in qikprops:
            qikprops[prop]
            if qikprops[prop] == '':   # QikProp has failed
                if fail == 0:
                    with open("./qikFail_eigv.txt", "a+") as results:
                        results.write("%s \n" % lig_name)
                    fail = 1
                out = [np.append(out[0], np.array(0))]
            else:
                out = [np.append(out[0], np.array(float(qikprops[prop])))]

        # Add Number of rotatable bonds
        for mol in pybel.readfile("sdf", lig_sdf): # can be sdf or mol2
            out = [np.append(out[0], np.array(mol.OBMol.NumRotors()))]

        # Add number of aromatic rings
        for mol in pybel.readfile("sdf", lig_sdf): # could be sdf or mol2
            result = ["Aromatic" for r in mol.OBMol.GetSSSR() if r.IsAromatic()]
            out = [np.append(out[0], np.array(len(result)))]

        print("Protein:")
        print(prot_pdb)

        pdb = parsePDB(prot_pdb)
        calphas = pdb.select('calpha')

        anm = ANM('pdb ANM analysis')
        anm.buildHessian(calphas, cutoff=12.0)
        anm.getHessian().round(3)
        anm.calcModes(n_modes = nmbr_modes)

        # Add NMA Length
        out = [np.append(out[0], np.array(len(anm.getEigvals())))]
        out = [np.append(out[0], np.array(len(anm.getEigvecs())))]

        return np.vstack(out)

    def __len__(self):
        """ Returns the dimensions of descriptors """
        if self.aligned_pairs:
            return len(self.ligand_types) * self.cutoff.shape[0]
        else:
            return len(self.ligand_types) * len(self.protein_types) * len(self.cutoff)

    def __reduce__(self):
        return close_contacts_descriptor, (self.protein,
                                           self.original_cutoff,
                                           self.mode,
                                           self.ligand_types,
                                           self.protein_types,
                                           self.aligned_pairs)


class universal_descriptor(object):
    def __init__(self,
                 func,
                 protein=None,
                 shape=None,
                 sparse=False):
        """An universal descriptor which converts a callable object (function)
        to a descriptor generator which can be used in scoring methods.

        .. versionadded:: 0.6

        Parameters
        ----------
        func: object
            A function to be mapped accross all ligands. Can be any callable
            object, which takes ligand as first argument and optionally
            protein key word argument. Additional arguments should be set
            using `functools.partial`.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        """
        self.func = func
        self.protein = protein
        self.shape = shape
        self.sparse = sparse
        if isinstance(func, partial):
            self.titles = self.func.func.__name__
        else:
            self.titles = self.func.__name__

    def build(self, ligands, protein=None):
        """Builds descriptors for series of ligands

        Parameters
        ----------
        ligands: iterable of oddt.toolkit.Molecules or oddt.toolkit.Molecule
            A list or iterable of ligands to build the descriptor or a
            single molecule.

        protein: oddt.toolkit.Molecule or None (default=None)
            Default protein to use as reference

        """
        if protein:
            self.protein = protein
        if is_molecule(ligands):
            ligands = [ligands]
        out = []
        for mol in ligands:
            if self.protein is None:
                out.append(self.func(mol))
            else:
                out.append(self.func(mol, protein=self.protein))
        if self.sparse:
            # out = list(map(partial(sparse_to_csr_matrix, size=self.shape), out))
            return sparse_vstack(map(partial(sparse_to_csr_matrix,
                                             size=self.shape), out),
                                 format='csr')
        else:
            return np.vstack(out)

    def __len__(self):
        """ Returns the dimensions of descriptors """
        if self.shape is None:
            raise NotImplementedError('The length of descriptor is not defined')
        else:
            return self.shape

    def __reduce__(self):
        return universal_descriptor, (self.func, self.protein, self.shape,
                                      self.sparse)


# TODO: we don't use toolkit. should we?
class fingerprints(object):
    def __init__(self, fp='fp2', toolkit='ob'):
        self.fp = fp
        self.exchange = False
        # if toolkit == oddt.toolkit.backend:
        #    self.exchange = False
        # else:
        #    self.exchange = True
        #    self.target_toolkit = __import__('toolkits.'+toolkit)

    def _get_fingerprint(self, mol):
        if self.exchange:
            mol = self.target_toolkit.Molecule(mol)
        return mol.calcfp(self.fp).raw

    def build(self, mols):
        if is_molecule(mols):
            mols = [mols]
        out = []
        for mol in mols:
            fp = self._get_fingerprint(mol)
            out.append(fp)
        return np.vstack(out)

    def __reduce__(self):
        return fingerprints, ()


class autodock_vina_descriptor(object):
    def __init__(self, protein=None, vina_scores=None):
        self.protein = protein
        self.vina = autodock_vina(protein)
        self.vina_scores = vina_scores or ['vina_affinity',
                                           'vina_gauss1',
                                           'vina_gauss2',
                                           'vina_repulsion',
                                           'vina_hydrophobic',
                                           'vina_hydrogen']
        self.titles = self.vina_scores

    def set_protein(self, protein):
        self.protein = protein
        self.vina.set_protein(protein)

    def build(self, ligands, protein=None):
        if protein:
            self.set_protein(protein)
        else:
            protein = self.protein
        if is_molecule(ligands):
            ligands = [ligands]
        desc = None
        for mol in ligands:
            # Vina
            # TODO: Asynchronous output from vina, push command to score and retrieve at the end?
            # TODO: Check if ligand has vina scores
            scored_mol = self.vina.score(mol)[0].data
            vec = np.array(([scored_mol[key] for key in self.vina_scores]),
                           dtype=np.float32).flatten()
            if desc is None:
                desc = vec
            else:
                desc = np.vstack((desc, vec))
        return np.atleast_2d(desc)

    def __len__(self):
        """ Returns the dimensions of descriptors """
        return len(self.vina_scores)

    def __reduce__(self):
        return autodock_vina_descriptor, (self.protein, self.vina_scores)


class oddt_vina_descriptor(object):
    def __init__(self, protein=None, vina_scores=None):
        self.protein = protein
        self.vina = vina_docking(protein)
        self.all_vina_scores = ['vina_affinity',
                                # inter-molecular interactions
                                'vina_gauss1',
                                'vina_gauss2',
                                'vina_repulsion',
                                'vina_hydrophobic',
                                'vina_hydrogen',
                                # intra-molecular interactions
                                'vina_intra_gauss1',
                                'vina_intra_gauss2',
                                'vina_intra_repulsion',
                                'vina_intra_hydrophobic',
                                'vina_intra_hydrogen',
                                'vina_num_rotors']
        self.vina_scores = vina_scores or self.all_vina_scores
        self.titles = self.vina_scores

    def set_protein(self, protein):
        self.protein = protein
        self.vina.set_protein(protein)

    def build(self, ligands, protein=None):
        if protein:
            self.set_protein(protein)
        else:
            protein = self.protein
        if is_molecule(ligands):
            ligands = [ligands]
        desc = None
        for mol in ligands:
            mol_keys = mol.data.keys()
            if any(x not in mol_keys for x in self.vina_scores):
                self.vina.set_ligand(mol)
                inter = self.vina.score_inter()
                intra = self.vina.score_intra()
                num_rotors = self.vina.num_rotors
                # could use self.vina.score(), but better to reuse variables
                affinity = ((inter * self.vina.weights[:5]).sum() /
                            (1 + self.vina.weights[5] * num_rotors))
                assert len(self.all_vina_scores) == len(inter) + len(intra) + 2
                score = dict(zip(
                    self.all_vina_scores,
                    np.hstack((affinity, inter, intra, num_rotors)).flatten()
                ))
                mol.data.update(score)
            else:
                score = mol.data.to_dict()
            try:
                vec = np.array([score[s] for s in self.vina_scores],
                               dtype=np.float32).flatten()
            except Exception as e:
                print(score, affinity, inter, intra, num_rotors)
                print(mol.title)
                raise e
            if desc is None:
                desc = vec
            else:
                desc = np.vstack((desc, vec))
        return np.atleast_2d(desc)

    def __len__(self):
        """ Returns the dimensions of descriptors """
        return len(self.vina_scores)

    def __reduce__(self):
        return oddt_vina_descriptor, (self.protein, self.vina_scores)
