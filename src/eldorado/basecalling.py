import re
import subprocess
from pathlib import Path
from typing import List

import hashlib

from src.eldorado.constants import DORADO_EXECUTABLE, MODELS_DIR, MODIFICATIONS
from src.eldorado.logging_config import logger
from src.eldorado.my_dataclasses import SequencingRun


def process_unbasecalled_pod5_dirs(pod5_dir: Path, dry_run: bool):

    run = SequencingRun.create_from_pod5_dir(pod5_dir)

    pod5_files_to_process = run.get_pod5_files_for_basecalling()

    # Skip if no pod5 files to basecall
    if not pod5_files_to_process:
        logger.info("No pod5 files to basecall in %s", pod5_dir)
        return

    # Create a md5 hash for batch
    string = "".join([str(x) for x in pod5_files_to_process])
    md5_hash = hashlib.md5(string.encode()).hexdigest()

    output_bam_batch = run.output_bam_parts_dir / f"batch_{md5_hash}.bam"
    output_bam_batch_manifest = output_bam_batch.parent / f"{output_bam_batch.name}.pod5_manifest.txt"
    script_file = run.script_dir / f"run_basecaller_batch_{md5_hash}.sh"

    lock_files = run.lock_files_from_list(pod5_files_to_process)
    done_files = run.done_files_from_list(pod5_files_to_process)

    # Submit job
    basecalling_model = get_basecalling_model(run)
    modified_bases_models = get_modified_bases_models(basecalling_model)
    submit_basecalling_to_slurm(
        basecalling_model=basecalling_model,
        modified_bases_models=modified_bases_models,
        pod5_files=pod5_files_to_process,
        output_bam=output_bam_batch,
        output_bam_manifest=output_bam_batch_manifest,
        dry_run=dry_run,
        script_file=script_file,
        lock_files=lock_files,
        done_files=done_files,
    )


def get_pod5_dirs_for_basecalling(pod5_dirs: List[Path]) -> List[Path]:

    # Keep only pod5 directories that has pod5 files
    pod5_dirs = [x for x in pod5_dirs if contains_pod5_files(x)]

    # Keep only pod5 dirs that are not done basecalling
    pod5_dirs = [x for x in pod5_dirs if not is_basecalling_complete(x)]

    return pod5_dirs


def contains_pod5_files(x: Path) -> bool:
    return any(x.glob("*.pod5"))


def is_basecalling_complete(pod5_dir: Path) -> bool:
    any_existing_bam_outputs = any(pod5_dir.parent.glob("bam*/*.bam"))
    any_existing_fastq_outputs = any(pod5_dir.parent.glob("fastq*/*.fastq*"))

    return any_existing_bam_outputs or any_existing_fastq_outputs


def compare_versions(version1, version2):
    """
    Compare two version strings.

    Returns:
        True if version1 => version2
        FALSE if version1 < version2
    """
    v1_components = [int(x) for x in version1.split(".")]
    v2_components = [int(x) for x in version2.split(".")]

    # Pad the shorter version with zeros
    while len(v1_components) < len(v2_components):
        v1_components.append(0)
    while len(v2_components) < len(v1_components):
        v2_components.append(0)

    # Compare each component
    for v1_component, v2_component in zip(v1_components, v2_components):
        if v1_component > v2_component:
            return True
        elif v1_component < v2_component:
            return False

    # If all components are equal, return False
    return False


def extract_version(path: Path) -> str:
    pattern = r"@v([\.\d+]{0,3})$"
    return re.findall(pattern, path.name)[0]


def get_latest_version(models: List[Path]) -> Path:
    """
    Find the latest version of a list of paths.
    """
    # Initialize
    latest_path = models[0]
    latest_version = extract_version(latest_path)

    # If there are more than one model in the list compare the versions
    for path in models[1:]:
        version = extract_version(path)
        if version and (latest_version is None or compare_versions(version, latest_version)):
            latest_version = version
            latest_path = path

    return latest_path


def get_basecalling_model(run: SequencingRun) -> Path:

    # Find basecalling model
    # Link to model documentation: https://github.com/nanoporetech/dorado?tab=readme-ov-file#dna-models
    basecalling_model = None

    # If run on FLO-PRO114M (R10.4.1) flow cell
    if run.flow_cell_product_code == "FLO-PRO114M":
        if run.sample_rate == 4000:
            basecalling_model = MODELS_DIR / "dna_r10.4.1_e8.2_400bps_hac@v4.1.0"
        elif run.sample_rate == 5000:
            basecalling_model = MODELS_DIR / "dna_r10.4.1_e8.2_400bps_hac@v4.3.0"

    # If run on FLO-PRO002 (R9.4.1) flow cell
    elif run.flow_cell_product_code == "FLO-PRO002":
        basecalling_model = MODELS_DIR / "dna_r9.4.1_e8_hac@v3.3"

    # Check if basecalling model was found
    if basecalling_model is None:
        raise ValueError(f"Could not find basecalling model for flow cell {run.flow_cell_product_code}")
    # Check if basecalling model exists
    if not basecalling_model.exists():
        raise ValueError(f"Basecalling model {basecalling_model} does not exist")

    return basecalling_model


def get_modified_bases_models(basecalling_model: Path) -> List[Path]:

    modified_bases_models = []

    # Get all modified base models based on base model
    for mod in MODIFICATIONS:
        mod_models = list(MODELS_DIR.glob(f"{basecalling_model.name}*{mod}*"))

        # If more than one model found, select the latest one and add it to the list
        if mod_models:
            latest_mod_model = get_latest_version(mod_models)
            modified_bases_models.append(latest_mod_model)

    return modified_bases_models


def submit_basecalling_to_slurm(
    script_file: Path,
    pod5_files: List[Path],
    basecalling_model: Path,
    modified_bases_models: List[Path],
    output_bam: Path,
    output_bam_manifest: Path,
    dry_run: bool,
    lock_files: List[Path],
    done_files: List[Path],
):

    # Convert path lists to strings
    pod5_files_str = " ".join([str(x) for x in pod5_files])
    lock_files_str = " ".join([str(x) for x in lock_files])
    done_files_str = " ".join([str(x) for x in done_files])

    # Construct SLURM job script
    modified_bases_models_arg = ""
    if modified_bases_models:
        modified_bases_models_str = ",".join([str(x) for x in modified_bases_models])
        modified_bases_models_arg = f"--modified-bases-models {modified_bases_models_str}"

    slurm_script = f"""\
#!/bin/bash
#SBATCH --account           MomaDiagnosticsHg38
#SBATCH --time              7-00:00:00
#SBATCH --cpus-per-task     18
#SBATCH --mem               190g
#SBATCH --partition         gpu
#SBATCH --gres              gpu:1
#SBATCH --mail-type         FAIL
#SBATCH --mail-user         simon.drue@clin.au.dk

        # Make sure .lock files are removed when job is done
        trap 'rm {lock_files_str}' EXIT
        
        set -eu

        # Log start time
        START=$(date '+%Y-%m-%d %H:%M:%S')
        START_S=$(date '+%s')

        # Create output directory
        OUTDIR=$(dirname {output_bam})
        mkdir -p $OUTDIR

        # Create temp output file
        TEMP_BAM_FILE=$(mktemp {output_bam}.tmp.XXXXXXXX)

        # Create temp directory with symlinks to pod5 files
        TEMP_POD5_DIR=$(mktemp -d ${{OUTDIR}}/temp_pod5_input.XXXXXXXX)
        
        POD5_FILES_LIST=({pod5_files_str})
        
        for pod5_file in ${{POD5_FILES_LIST[@]}}
        do
            ln -s ${{pod5_file}} ${{TEMP_POD5_DIR}}
        done

        # Trap temp files
        trap 'rm ${{TEMP_BAM_FILE}}' EXIT
        trap 'rm -r ${{TEMP_POD5_DIR}}' EXIT

        # Run basecaller
        {DORADO_EXECUTABLE} basecaller \\
            --no-trim \\
            {modified_bases_models_arg} \\
            {basecalling_model} \\
            {pod5_files_str} \\
        > ${{TEMP_BAM_FILE}}

        # Move temp file to output 
        mv ${{TEMP_BAM_FILE}} {output_bam}

        # Log end time
        END=$(date '+%Y-%m-%d %H:%M:%S')
        END_S=$(date +%s)
        RUNTIME=$((END_S-START_S))

        # Get size of input and output
        POD5_SIZE=$(du -sL {pod5_files_str} | cut -f1)
        POD5_COUNT=$(ls -1 {pod5_files_str} | grep ".pod5" | wc -l)
        OUTPUT_BAM_SIZE=$(du -sL {output_bam} | cut -f1)

        # Write log file
        LOG_FILE={output_bam}.eldorado.basecaller.log
        echo "pod5_size=$POD5_SIZE" >> ${{LOG_FILE}}
        echo "pod5_count=$POD5_COUNT" >> ${{LOG_FILE}}
        echo "output_bam={output_bam}" >> ${{LOG_FILE}}
        echo "output_bam_size=$OUTPUT_BAM_SIZE" >> ${{LOG_FILE}}
        echo "slurm_job_id=$SLURM_JOB_ID" >> ${{LOG_FILE}}
        echo "start=$START" >> ${{LOG_FILE}}
        echo "end=$END" >> ${{LOG_FILE}}
        echo "runtime=$RUNTIME" >> ${{LOG_FILE}}
        echo "basecaller={DORADO_EXECUTABLE}" >> ${{LOG_FILE}}
        echo "basecalling_model={basecalling_model}" >> ${{LOG_FILE}}
        echo "modified_bases_models={modified_bases_models}" >> ${{LOG_FILE}}

        # Write pod5 manifest and touch done files
        for pod5_file in ${{POD5_FILES_LIST[@]}}
        do
            echo ${{pod5_file}} >> {output_bam_manifest}
        done
        
        # Touch done files
        touch {done_files_str}

    """

    # Write Slurm script to a file
    script_file.parent.mkdir(exist_ok=True, parents=True)
    with open(script_file, "w", encoding="utf-8") as f:
        logger.info("Writing Slurm script to %s", script_file)
        f.write(slurm_script)

    if dry_run:
        logger.info("Dry run. Skipping submission of basecalling job.")
        return

    # Create .lock files
    for lock_file in lock_files:
        lock_file.parent.mkdir(exist_ok=True, parents=True)
        lock_file.touch()

    # Submit the job using Slurm
    subprocess.run(["sbatch", script_file], check=True)