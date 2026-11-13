# philani_fmri_process: the C-PAC image plus a T1w defacer. Built on each computer by
# setup_docker_image.ps1 (run by the installer), never pushed to a registry:
#   docker build --build-arg DEFACER=<freesurfer|local|pydeface> -t philani_fmri_process:latest .
# BuildKit only builds the stages the chosen DEFACER needs, so 'local' and 'pydeface' never
# download FreeSurfer. The 'philani.defacer' label tells processing/deface.py what it holds.

ARG CPAC_IMAGE=fcpindi/c-pac:release-v1.8.7
ARG FREESURFER_IMAGE=freesurfer/freesurfer:7.1.1
ARG DEFACER=freesurfer

FROM ${FREESURFER_IMAGE} AS freesurfer-release

FROM ${CPAC_IMAGE} AS cpac

# mri_deface + its templates out of the official FreeSurfer image; nothing else is kept.
# The license it may need is the one C-PAC already ships ($FREESURFER_HOME/license.txt).
FROM cpac AS defacer-freesurfer
COPY --from=freesurfer-release /usr/local/freesurfer/bin/mri_deface /opt/mri_deface/
COPY --from=freesurfer-release \
    /usr/local/freesurfer/average/talairach_mixed_with_skull.gca \
    /usr/local/freesurfer/average/face.gca \
    /opt/mri_deface/
LABEL philani.defacer=mri_deface

# The user's own mri_deface + templates, copied into deface_files/ by the setup script.
FROM cpac AS defacer-local
COPY deface_files/ /opt/mri_deface/
RUN chmod 755 /opt/mri_deface/mri_deface
LABEL philani.defacer=mri_deface

# pydeface (MIT) uses the FSL flirt already in the C-PAC image.
FROM cpac AS defacer-pydeface
RUN pip install --no-cache-dir pydeface==2.0.2
LABEL philani.defacer=pydeface

FROM defacer-${DEFACER}
