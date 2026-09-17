import os
import argparse
from huggingface_hub import snapshot_download

def download_qwen_model(model_id="Qwen/Qwen2.5-VL-3B-Instruct", local_dir="./models/qwen"):
    """
    Downloads open-weights Qwen Vision-Language model from Hugging Face hub into a local folder.
    No API keys or cloud service required. Runs 100% locally.
    """
    print(f"==================================================")
    print(f" Downloading Local Hugging Face Qwen Model")
    print(f" Model ID : {model_id}")
    print(f" Target   : {os.path.abspath(local_dir)}")
    print(f"==================================================")

    os.makedirs(local_dir, exist_ok=True)
    
    # Download weights & configs into local directory
    snapshot_download(
        repo_id=model_id,
        local_dir=local_dir,
        local_dir_use_symlinks=False,
        resume_download=True
    )
    
    print("\n[SUCCESS] Download complete! Qwen model files stored in:", os.path.abspath(local_dir))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download Qwen model from Hugging Face")
    parser.add_argument("--model", type=str, default="Qwen/Qwen2.5-VL-3B-Instruct", help="Hugging Face Model ID (e.g. Qwen/Qwen2.5-VL-3B-Instruct)")
    parser.add_argument("--dir", type=str, default="./models/qwen", help="Local directory to store model files")
    args = parser.parse_args()

    download_qwen_model(model_id=args.model, local_dir=args.dir)
