#!/usr/bin/env python3
"""
Test script to check access to the exact PROD Snowflake secret
"""

import boto3
import json
from botocore.exceptions import ClientError

def test_secret_access():
    """Test access to the exact PROD secret that's failing"""
    
    secret_name = "prod/carsupermarket/core/snowflake/data_analyst_credentials"
    region_name = "eu-west-1"
    
    print(f"🔍 Testing access to secret: '{secret_name}'")
    print(f"🌍 Region: {region_name}")
    print(f"🔑 Using your current AWS credentials...")
    print("-" * 60)
    
    try:
        # Create secrets manager client
        session = boto3.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name=region_name
        )
        
        print(f"✅ Created SecretsManager client for {region_name}")
        
        # Try to get the secret
        print(f"🔍 Attempting to retrieve secret...")
        response = client.get_secret_value(SecretId=secret_name)
        
        # Success!
        print(f"✅ SUCCESS: Secret retrieved successfully!")
        secret_string = response['SecretString']
        secret_data = json.loads(secret_string)
        
        # Show what we got (without sensitive data)
        print(f"📋 Secret contains {len(secret_data)} keys:")
        for key in secret_data.keys():
            if key.lower() == 'password':
                print(f"   - {key}: ***HIDDEN***")
            else:
                print(f"   - {key}: {secret_data[key]}")
        
        return True
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        
        print(f"❌ AWS ClientError: {error_code}")
        print(f"❌ Error message: {error_message}")
        
        if error_code == 'ResourceNotFoundException':
            print(f"🔍 DIAGNOSIS: Secret doesn't exist at that path/region")
            print(f"🔍 SUGGESTION: Double-check the secret name and region")
        elif error_code == 'AccessDeniedException':
            print(f"🔍 DIAGNOSIS: Your AWS credentials don't have permission")
            print(f"🔍 SUGGESTION: You need secretsmanager:GetSecretValue permission for PROD secrets")
        elif error_code == 'UnauthorizedOperation':
            print(f"🔍 DIAGNOSIS: IAM permissions issue")
            print(f"🔍 SUGGESTION: Check your IAM user/role permissions")
        else:
            print(f"🔍 DIAGNOSIS: Unknown error - see AWS documentation for {error_code}")
        
        return False
        
    except Exception as e:
        print(f"❌ Unexpected error: {type(e).__name__}")
        print(f"❌ Error message: {str(e)}")
        return False

def check_aws_identity():
    """Check what AWS identity we're using"""
    try:
        sts_client = boto3.client('sts')
        identity = sts_client.get_caller_identity()
        
        print(f"🔑 Your AWS Identity:")
        print(f"   - Account: {identity.get('Account', 'Unknown')}")
        print(f"   - User ARN: {identity.get('Arn', 'Unknown')}")
        print(f"   - User ID: {identity.get('UserId', 'Unknown')}")
        
    except Exception as e:
        print(f"❌ Could not get AWS identity: {str(e)}")

if __name__ == "__main__":
    print("🚀 PROD Secret Access Test")
    print("=" * 60)
    
    # First check what AWS identity we're using
    check_aws_identity()
    print()
    
    # Test the secret access
    success = test_secret_access()
    
    print()
    print("=" * 60)
    if success:
        print("✅ RESULT: You have access to the PROD secret!")
        print("🤔 If Snowflake still fails, the issue is elsewhere.")
    else:
        print("❌ RESULT: You don't have access to the PROD secret.")
        print("💡 This is normal for developers - use DEV environment for testing.")

# Alternative quick CLI test:
# aws secretsmanager get-secret-value --secret-id "prod/carsupermarket/core/snowflake/data_analyst_credentials" --region eu-west-1