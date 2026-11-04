from typing import Optional
from pydantic import EmailStr
from sqlmodel import SQLModel

class EmailSchema(SQLModel):
    email: EmailStr

class LoginSchema(SQLModel):
    email: EmailStr
    code: str

class Token(SQLModel):
    access_token: str
    token_type: str

class TokenData(SQLModel):
    email: Optional[str] = None
