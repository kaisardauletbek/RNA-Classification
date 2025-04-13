#!/usr/bin/env python3

"""
mint_age_pipeline_modular.py

A modular version of the MINT-AGE style pipeline:
1) Parse data from PDB files
2) Procrustes alignment
3) Pre-clustering (average linkage, outlier detection)
4) Post-clustering (torus PCA + mode hunting)
5) (Optional) plotting
"""

import os
import pickle
from collections import Counter

import numpy as np
from matplotlib import pyplot as plt
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import average, fcluster

# ---------------------------------------------------------------------
# Imports from your codebase (update paths to match your structure)
# ---------------------------------------------------------------------
# shape_analysis/shape_analysis.py
from shape_analysis.shape_analysis import procrustes_analysis

# pnds/PNDS_RNA_clustering.py
from pnds.PNDS_RNA_clustering import new_multi_slink

# utils/help_plot_functions.py
from utils.help_plot_functions import plot_clustering

# utils/data_functions.py
from utils.data_functions import rotate_y_optimal_to_x, rotation_matrix_x_axis

# parsing/parse_functions.py
from parsing.parse_functions import parse_pdb_files


def parse_data(input_pdb_dir):
    """
    Step 0: Parse data from a local directory containing PDB files.
    Uses parse_pdb_files(...) from your code.
    Returns a list of 'suite' objects (or similar) extracted from PDBs.
    """
    print("[MINT-AGE] Parsing data from:", input_pdb_dir)
    suites = parse_pdb_files(input_pdb_dir)
    print(f"Parsed {len(suites)} suite objects.")
    return suites


def procrustes_step(suites, output_folder, recalculate=False):
    """
    Step 1: Perform Procrustes alignment on the suite objects.
    We can load pre-aligned data from a pickle if recalculate=False
    or just do it fresh if recalculate=True or no file found.
    """
    print("[MINT-AGE] Step 1: Procrustes alignment...")

    procrustes_file = os.path.join(output_folder, "procrustes_suites.pickle")

    if recalculate:
        # Re-run procrustes alignment
        suites = procrustes_analysis(suites, overwrite=True)
        with open(procrustes_file, "wb") as f:
            pickle.dump(suites, f)
    else:
        # Attempt to load from file
        if os.path.isfile(procrustes_file):
            with open(procrustes_file, "rb") as f:
                suites = pickle.load(f)
            print("[MINT-AGE] Loaded pre-aligned suites from pickle.")
        else:
            # If no file, run alignment anyway
            suites = procrustes_analysis(suites, overwrite=True)
            with open(procrustes_file, "wb") as f:
                pickle.dump(suites, f)

    return suites


def AGE(
    suites,
    method=average,
    outlier_percentage=0.15,
    min_cluster_size=20,
):
    """
    Step 2: Pre-clustering using average linkage + simple outlier detection.

    We:
     - Extract dihedral angles (or any other representation) from each suite
     - Compute distances (e.g. Euclidean for demonstration)
     - Link them (method=average by default)
     - Determine a threshold to flag ~ 'outlier_percentage' fraction as outliers
     - Cut tree at that threshold
     - Return the set of cluster indices for further refinement.
    """
    print("[MINT-AGE] Step 2: Average-linkage pre-clustering + outlier detection...")

    # Filter out suites lacking ._dihedral_angles
    dihedral_data = np.array([s._dihedral_angles for s in suites if s._dihedral_angles is not None])
    suite_indices = [i for i, s in enumerate(suites) if s._dihedral_angles is not None]

    # Distances (Euclidean placeholder; replace with torus if needed)
    dist_vec = pdist(dihedral_data, metric="euclidean")

    # Linkage
    linkage_matrix = method(dist_vec)

    # Determine threshold
    threshold = find_outlier_threshold_simple(
        linkage_matrix,
        percentage=outlier_percentage,
        data_count=dihedral_data.shape[0],
        min_cluster_size=min_cluster_size
    )

    # Cluster labels
    cluster_labels = fcluster(linkage_matrix, threshold, criterion="distance")

    # Mark outliers as those in clusters < min_cluster_size
    cluster_counts = Counter(cluster_labels)
    outlier_clusters = [c for c, count in cluster_counts.items() if count < min_cluster_size]
    outlier_indices = [i for i, c in enumerate(cluster_labels) if c in outlier_clusters]

    # Build final cluster list (list of lists)
    cluster_list = []
    for c, count in cluster_counts.items():
        if count >= min_cluster_size:
            idxs = [suite_indices[i] for i, lab in enumerate(cluster_labels) if lab == c]
            cluster_list.append(idxs)

    # Label suites accordingly
    for c_idx, clust in enumerate(cluster_list):
        for sidx in clust:
            if not hasattr(suites[sidx], "clustering"):
                suites[sidx].clustering = {}
            suites[sidx].clustering["precluster"] = c_idx
    for sidx in outlier_indices:
        if not hasattr(suites[suite_indices[sidx]], "clustering"):
            suites[suite_indices[sidx]].clustering = {}
        suites[suite_indices[sidx]].clustering["precluster"] = "outlier"

    return cluster_list


def MINT(suites, cluster_list):
    """
    Step 3: Post-clustering (Torus PCA + mode hunting).
    Here we call 'new_multi_slink' from PNDS_RNA_clustering on each cluster.
    Returns a list of "refined" cluster index sets.
    """
    print("[MINT-AGE] Step 3: Torus PCA + mode hunting on each pre-cluster...")

    refined_clusters = []
    for clust_indices in cluster_list:
        # Gather dihedral angles for this cluster
        cluster_data = [suites[i]._dihedral_angles for i in clust_indices]
        if len(cluster_data) == 0:
            continue
        cluster_data = np.array(cluster_data)

        # new_multi_slink can return subclusters + noise
        # scale=12000 is domain-specific, adapt as needed
        subclusters, noise = new_multi_slink(
            scale=12000,
            data=cluster_data,
            cluster_list=[list(range(len(cluster_data)))],
            outlier_list=[]
        )
        # Map subclusters back to original suite indices
        for sc in subclusters:
            refined_clusters.append([clust_indices[idx] for idx in sc])

    # Label final clusters
    final_label = 0
    for clust in refined_clusters:
        for sidx in clust:
            if not hasattr(suites[sidx], "clustering"):
                suites[sidx].clustering = {}
            suites[sidx].clustering["mint_age_cluster"] = final_label
        final_label += 1

    return refined_clusters


def find_outlier_threshold_simple(linkage_matrix, percentage, data_count, min_cluster_size):
    """
    A simplified approach:
    - Sort distances in ascending order
    - For each distance 'd', cut the tree and see how many points
      belong to clusters of size < min_cluster_size.
    - If that fraction is <= percentage, we pick 'd'.
    """
    if percentage <= 0:
        return linkage_matrix[-1, 2] + 1.0

    distances = linkage_matrix[:, 2]
    for d in np.sort(distances):
        labs = fcluster(linkage_matrix, d, criterion='distance')
        counts = Counter(labs)
        small_cluster_pts = sum(cnt for cnt in counts.values() if cnt < min_cluster_size)
        outlier_frac = small_cluster_pts / data_count
        if outlier_frac <= percentage:
            return d
    return linkage_matrix[-1, 2] + 1.0


def run_mint_age_pipeline(
    input_pdb_dir,
    output_folder="./out/mint_age_pipeline",
    recalculate=False,
    min_cluster_size=20,
    outlier_percentage=0.15,
    method=average,
    plot=True
):
    """
    Main assembler function for the entire MINT-AGE pipeline:
    0) parse_data
    1) procrustes_step
    2) AGE pre-clustering step
    3) MINT post_clustering_step
    4) optional plotting
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    # Step 0: parse data
    suites = parse_data(input_pdb_dir)

    # Step 1: Procrustes
    suites = procrustes_step(suites, output_folder, recalculate=recalculate)

    # Step 2: Pre-clustering
    cluster_list = AGE(
        suites,
        method=method,
        outlier_percentage=outlier_percentage,
        min_cluster_size=min_cluster_size,
    )

    # Step 3: Post-clustering
    refined_clusters = MINT(suites, cluster_list)

    # Step 4: (Optional) Plot final clusters
    if plot:
        final_plot_dir = os.path.join(output_folder, "final_plots")
        print("[MINT-AGE] Plotting final refined clusters...")
        plot_clustering(
            suite_list=suites,
            cluster_list=refined_clusters,
            out_dir=final_plot_dir
        )

    # Save final results if desired
    final_pickle = os.path.join(output_folder, "mint_age_final_suites.pickle")
    with open(final_pickle, "wb") as f:
        pickle.dump(suites, f)

    print(f"[MINT-AGE] Pipeline complete. Saved final suites to {final_pickle}")
    print(f"[MINT-AGE] Total final clusters: {len(refined_clusters)}")
    return suites


if __name__ == "__main__":
    """
    Example usage when running directly:
      python mint_age.py path/to/pdb_dir
    """
    # import sys

    # if len(sys.argv) < 2:
    #     print("Usage: python mint_age_pipeline_modular.py <pdb_dir>")
    #     sys.exit(1)

    # pdb_dir = sys.argv[1]
    pdb_dir = "/Users/kaisardauletbek/Documents/GitHub/RNA-Classification/data/rna2020_pruned_pdbs"

    final_suites = run_mint_age_pipeline(
        input_pdb_dir=pdb_dir,
        output_folder="/Users/kaisardauletbek/Documents/GitHub/RNA-Classification/results/mint_age_pipeline",
        recalculate=True,
        min_cluster_size=20,
        outlier_percentage=0.15,
        method=average,
        plot=True
    )

    print("[MINT-AGE] Done.")
