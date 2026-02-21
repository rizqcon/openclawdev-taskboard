"""
File uploads: image upload endpoint.
"""

import base64 as b64_module
import uuid

from fastapi import APIRouter, HTTPException

from app.config import DATA_DIR
from app.models import ImageUpload

router = APIRouter()


@router.post("/api/upload/image")
async def upload_image(image: ImageUpload):
    """Upload a base64 image and return the file path."""
    attachments_dir = DATA_DIR / "attachments"
    attachments_dir.mkdir(exist_ok=True)

    try:
        data = image.data
        if data.startswith("data:") and ";base64," in data:
            header, b64_content = data.split(",", 1)
            mime_type = header.split(":")[1].split(";")[0]
            ext = mime_type.split("/")[1] if "/" in mime_type else "png"
        else:
            b64_content = data
            ext = "png"

        if ext not in ["png", "jpg", "jpeg", "gif", "webp"]:
            ext = "png"

        img_filename = f"{uuid.uuid4().hex[:8]}_{image.filename or 'image'}"
        if not img_filename.endswith(f".{ext}"):
            img_filename = f"{img_filename}.{ext}"

        img_path = attachments_dir / img_filename
        with open(img_path, "wb") as f:
            f.write(b64_module.b64decode(b64_content))

        return {"path": f"/app/data/attachments/{img_filename}", "filename": img_filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to save image: {e}")
