from datetime import datetime
from typing import Optional

from odmantic import Model, Field
from pydantic import BaseModel, EmailStr


# Main ODM models for database
class User(Model):
    email: EmailStr
    username: str
    hashed_password: str
    db_name: str
    db_uri: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = False

    model_config = {"collection": "users"}


# Pydantic models for API requests/responses
class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str
    db_name: str
    db_uri: str


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    username: str
    created_at: datetime
    is_active: bool


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None
