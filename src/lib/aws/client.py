""""skeleton for AWS client, needs more functionality for later"""

import os

import boto3
import logging
from typing import Optional, Dict, Any
from botocore.exceptions import ClientError
from dotenv import load_dotenv



load_dotenv()
logger = logging.getLogger(__name__)

class AWSClient:
    def __init__(self, region: str = "us-east-1"):
        self.region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self.session = boto3.Session(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=self.region
        )

    def ecs(self):
        return self.session.client("ecs")
    
    def ec2(self):
        return self.session.client("ec2")
    
    def s3(self):
        return self.session.client("s3")
    
    def iam(self):
        return self.session.client("iam")
    
    def cloudwatch(self):
        return self.session.client("cloudwatch")
    
    def acm(self):
        return self.session.client("acm")
    
    def elbv2(self):
        return self.session.client("elbv2")
    
    def get_account_id(self) -> str:
        sts = self.session.client("sts")
        return sts.get_caller_identity()["Account"]
    
    def check_permissions(self) -> bool:
        try:
            self.get_account_id()
            logger.info("AWS credentials valid")
            return True
        except ClientError as e:
            logger.error(f"AWS credentials invalid: {e}")
            return False