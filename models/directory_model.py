from pydantic import BaseModel


class DirectoryModel(BaseModel):
    drive: str = ""
    subfolder: str = ""
    dirKey: str = ""
    name: str = ""
    title: str = ""
    thumbUrl: str = ""
