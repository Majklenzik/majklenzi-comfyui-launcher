# RunPod template: hodnoty na vyplnenie

Template vytváraš v RunPod: **Manage → Templates → New Template**. Obraz (image) musí byť najprv postavený
cez GitHub Actions a balík musí byť verejný (kroky sú v README.md).

| Pole v RunPod | Hodnota |
|---|---|
| Template name | Tvoj názov, napr. `majklenzi ComfyUI Launcher` |
| Template type | Pod |
| Compute type | NVIDIA GPU |
| Container image | `ghcr.io/<tvoje-github-meno>/<nazov-repa>:latest` (všetko malými písmenami) |
| Container registry credentials | prázdne (balík je verejný) |
| Container disk | 30 GB |
| Volume disk | 100 GB alebo viac (modely sú veľké) |
| Volume mount path | `/workspace` |
| Expose HTTP ports | `3000, 8188, 8888` |
| Expose TCP ports | `22` (len ak chceš SSH) |
| Container start command | prázdne (použije sa `/start.sh` z obrazu) |
| Template visibility | Private, kým to neotestuješ. Potom Public, ak ho chceš zdieľať. |

## Environment variables

| Premenná | Povinná | Čo robí |
|---|---|---|
| `LAUNCHER_PASSWORD` | odporúčané | Heslo do launchera na porte 3000 (prehliadač si vypýta heslo, meno je ľubovoľné). Používaj dlhé heslo, aspoň 12 znakov. Keď ju nenastavíš, launcher si pri prvom štarte **vygeneruje náhodné heslo**, vypíše ho do logov podu (záložka Logs) a uloží do `/workspace/.launcher/launcher_password.txt`. |
| `LAUNCHER_ALLOW_NO_AUTH` | nie | `1` vypne heslo úplne. Neodporúčam: launcher inštaluje kód do podu, takže kto pozná odkaz, má pod v rukách. |
| `JUPYTER_PASSWORD` | odporúčané | Token do Jupyteru na porte 8888. Bez nej sa vygeneruje náhodný a vypíše sa do logov podu. |
| `HF_TOKEN` | nie | Hugging Face token pre celý pod. Dá sa zadať aj neskôr priamo v UI. Pre tajnú hodnotu použi RunPod Secret: `{{ RUNPOD_SECRET_hf_token }}`. |
| `CIVITAI_TOKEN` | nie | To isté pre CivitAI. |
| `LAUNCHER_TITLE` | nie | Nadpis v launcheri, napr. názov tvojej značky. |
| `COMFY_ARGS` | nie | Ďalšie argumenty pre ComfyUI. Predvolene žiadne. `--enable-manager` zapne ComfyUI-Manager, ale port 8188 nemá heslo, takže by každý s odkazom mohol cez Manager inštalovať nody. |
| `AUTO_START_COMFY` | nie | `0` = ComfyUI sa nespustí samo, spustíš ho tlačidlom v launcheri. |

## Pri deployi

Na stránke Deploy otvor **Additional filters** a nastav **CUDA version** na 12.8 alebo novšiu. Obraz používa
PyTorch pre CUDA 12.8 a potrebuje ovládač NVIDIA, ktorý to podporuje.

## Čo uvidí používateľ

Po nasadení podu: port **3000** otvorí launcher (workflowy, vlastné modely, vlastné nody), port **8188** je ComfyUI,
port **8888** je JupyterLab. Prvý štart trvá dlhšie, lebo sa ComfyUI kopíruje na volume.

## Text do poľa "Template readme" (anglicky, pre ostatných používateľov)

```
ComfyUI with a point-and-click launcher.

Ports
- 3000  Launcher: install workflows with their custom nodes and models, add models by link
        (Hugging Face / CivitAI, token supported), install custom nodes from GitHub.
- 8188  ComfyUI
- 8888  JupyterLab

First start copies ComfyUI to /workspace, so use a volume of 100 GB or more. Everything you install is kept
on the volume. In "Additional filters" pick CUDA 12.8 or newer.

Optional environment variables
- LAUNCHER_PASSWORD  password for the launcher (a random one is generated and printed in the pod logs if unset)
- JUPYTER_PASSWORD   token for JupyterLab (recommended)
- HF_TOKEN           Hugging Face token (can also be pasted in the launcher)
```
