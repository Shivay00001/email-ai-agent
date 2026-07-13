from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, func
from database import Base

class Setting(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True)
    value = Column(Text)

class EmailLog(Base):
    __tablename__ = "email_logs"
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String, unique=True, index=True)
    thread_id = Column(String, index=True)
    sender_email = Column(String, index=True)
    subject = Column(String)
    content = Column(Text)
    is_outbound = Column(Boolean, default=False)
    status = Column(String, default="received") # "received", "draft", "approved", "sent"
    timestamp = Column(DateTime, default=func.now())
