import json
import sys
from itertools import groupby
from operator import itemgetter

from arnparse import arnparse
from boto3 import client, session
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session

from config.config import aws_services_json_file, json_asff_file_suffix, timestamp_utc
from lib.arn.arn import arn_parsing
from lib.logger import logger
from lib.outputs.models import Check_Output_JSON_ASFF
from lib.utils.utils import open_file, parse_json_file
from providers.aws.models import (
    AWS_Assume_Role,
    AWS_Audit_Info,
    AWS_Credentials,
    AWS_Organizations_Info,
)


################## AWS PROVIDER
class AWS_Provider:
    def __init__(self, audit_info):
        logger.info("Instantiating aws provider ...")
        self.aws_session = self.set_session(audit_info)
        self.role_info = audit_info.assumed_role_info

    def get_session(self):
        return self.aws_session

    def set_session(self, audit_info):
        try:
            if audit_info.credentials:
                # If we receive a credentials object filled is coming form an assumed role, so renewal is needed
                logger.info("Creating session for assumed role ...")
                # From botocore we can use RefreshableCredentials class, which has an attribute (refresh_using)
                # that needs to be a method without arguments that retrieves a new set of fresh credentials
                # asuming the role again. -> https://github.com/boto/botocore/blob/098cc255f81a25b852e1ecdeb7adebd94c7b1b73/botocore/credentials.py#L395
                assumed_refreshable_credentials = RefreshableCredentials(
                    access_key=audit_info.credentials.aws_access_key_id,
                    secret_key=audit_info.credentials.aws_secret_access_key,
                    token=audit_info.credentials.aws_session_token,
                    expiry_time=audit_info.credentials.expiration,
                    refresh_using=self.refresh,
                    method="sts-assume-role",
                )
                # Here we need the botocore session since it needs to use refreshable credentials
                assumed_botocore_session = get_session()
                assumed_botocore_session._credentials = assumed_refreshable_credentials
                assumed_botocore_session.set_config_variable(
                    "region", audit_info.profile_region
                )

                return session.Session(
                    profile_name=audit_info.profile,
                    botocore_session=assumed_botocore_session,
                )
            # If we do not receive credentials start the session using the profile
            else:
                logger.info("Creating session for not assumed identity ...")
                return session.Session(profile_name=audit_info.profile)
        except Exception as error:
            logger.critical(f"{error.__class__.__name__} -- {error}")
            sys.exit()

    # Refresh credentials method using assume role
    # This method is called "adding ()" to the name, so it cannot accept arguments
    # https://github.com/boto/botocore/blob/098cc255f81a25b852e1ecdeb7adebd94c7b1b73/botocore/credentials.py#L570
    def refresh(self):
        logger.info("Refreshing assumed credentials...")

        response = assume_role(self.role_info)
        refreshed_credentials = dict(
            # Keys of the dict has to be the same as those that are being searched in the parent class
            # https://github.com/boto/botocore/blob/098cc255f81a25b852e1ecdeb7adebd94c7b1b73/botocore/credentials.py#L609
            access_key=response["Credentials"]["AccessKeyId"],
            secret_key=response["Credentials"]["SecretAccessKey"],
            token=response["Credentials"]["SessionToken"],
            expiry_time=response["Credentials"]["Expiration"].isoformat(),
        )
        logger.info("Refreshed Credentials:")
        logger.info(refreshed_credentials)
        return refreshed_credentials


def provider_set_session(
    input_profile,
    input_role,
    input_session_duration,
    input_external_id,
    input_regions,
    organizations_role_arn,
):

    # Mark variable that stores all the info about the audit as global
    global current_audit_info

    assumed_session = None

    # Setting session
    current_audit_info = AWS_Audit_Info(
        original_session=None,
        audit_session=None,
        audited_account=None,
        audited_partition=None,
        profile=input_profile,
        profile_region=None,
        credentials=None,
        assumed_role_info=AWS_Assume_Role(
            role_arn=None,
            session_duration=None,
            external_id=None,
        ),
        audited_regions=input_regions,
        organizations_metadata=None,
    )

    logger.info("Generating original session ...")
    # Create an global original session using only profile/basic credentials info
    current_audit_info.original_session = AWS_Provider(current_audit_info).get_session()
    logger.info("Validating credentials ...")
    # Verificate if we have valid credentials
    caller_identity = validate_credentials(current_audit_info.original_session)

    logger.info("Credentials validated")
    logger.info(f"Original caller identity UserId : {caller_identity['UserId']}")
    logger.info(f"Original caller identity ARN : {caller_identity['Arn']}")

    current_audit_info.audited_account = caller_identity["Account"]
    current_audit_info.audited_partition = arnparse(caller_identity["Arn"]).partition

    logger.info("Checking if organizations role assumption is needed ...")
    if organizations_role_arn:
        current_audit_info.assumed_role_info.role_arn = organizations_role_arn
        current_audit_info.assumed_role_info.session_duration = input_session_duration

        # Check if role arn is valid
        try:
            # this returns the arn already parsed, calls arnparse, into a dict to be used when it is needed to access its fields
            role_arn_parsed = arn_parsing(current_audit_info.assumed_role_info.role_arn)

        except Exception as error:
            logger.critical(f"{error.__class__.__name__} -- {error}")
            sys.exit()

        else:
            logger.info(
                f"Getting organizations metadata for account {organizations_role_arn}"
            )
            assumed_credentials = assume_role(current_audit_info)
            current_audit_info.organizations_metadata = get_organizations_metadata(
                current_audit_info.audited_account, assumed_credentials
            )
            logger.info(f"Organizations metadata retrieved")

    logger.info("Checking if role assumption is needed ...")
    if input_role:
        current_audit_info.assumed_role_info.role_arn = input_role
        current_audit_info.assumed_role_info.session_duration = input_session_duration
        current_audit_info.assumed_role_info.external_id = input_external_id

        # Check if role arn is valid
        try:
            # this returns the arn already parsed, calls arnparse, into a dict to be used when it is needed to access its fields
            role_arn_parsed = arn_parsing(current_audit_info.assumed_role_info.role_arn)

        except Exception as error:
            logger.critical(f"{error.__class__.__name__} -- {error}")
            sys.exit()

        else:
            logger.info(
                f"Assuming role {current_audit_info.assumed_role_info.role_arn}"
            )
            # Assume the role
            assumed_role_response = assume_role(current_audit_info)
            logger.info("Role assumed")
            # Set the info needed to create a session with an assumed role
            current_audit_info.credentials = AWS_Credentials(
                aws_access_key_id=assumed_role_response["Credentials"]["AccessKeyId"],
                aws_session_token=assumed_role_response["Credentials"]["SessionToken"],
                aws_secret_access_key=assumed_role_response["Credentials"][
                    "SecretAccessKey"
                ],
                expiration=assumed_role_response["Credentials"]["Expiration"],
            )
            assumed_session = AWS_Provider(current_audit_info).get_session()

    if assumed_session:
        logger.info("Audit session is the new session created assuming role")
        current_audit_info.audit_session = assumed_session
        current_audit_info.audited_account = role_arn_parsed.account_id
        current_audit_info.audited_partition = role_arn_parsed.partition
    else:
        logger.info("Audit session is the original one")
        current_audit_info.audit_session = current_audit_info.original_session

    # Setting default region of session
    if current_audit_info.audit_session.region_name:
        current_audit_info.profile_region = current_audit_info.audit_session.region_name
    else:
        current_audit_info.profile_region = "us-east-1"

    return current_audit_info


def validate_credentials(validate_session: session) -> dict:
    try:
        validate_credentials_client = validate_session.client("sts")
        caller_identity = validate_credentials_client.get_caller_identity()
    except Exception as error:
        logger.critical(f"{error.__class__.__name__} -- {error}")
        sys.exit()
    else:
        return caller_identity


def assume_role(audit_info: AWS_Audit_Info) -> dict:
    try:
        # set the info to assume the role from the partition, account and role name
        sts_client = audit_info.original_session.client("sts")
        # If external id, set it to the assume role api call
        if audit_info.assumed_role_info.external_id:
            assumed_credentials = sts_client.assume_role(
                RoleArn=audit_info.assumed_role_info.role_arn,
                RoleSessionName="ProwlerProAsessmentSession",
                DurationSeconds=audit_info.assumed_role_info.session_duration,
                ExternalId=audit_info.assumed_role_info.external_id,
            )
        # else assume the role without the external id
        else:
            assumed_credentials = sts_client.assume_role(
                RoleArn=audit_info.assumed_role_info.role_arn,
                RoleSessionName="ProwlerProAsessmentSession",
                DurationSeconds=audit_info.assumed_role_info.session_duration,
            )
    except Exception as error:
        logger.critical(f"{error.__class__.__name__} -- {error}")
        sys.exit()

    else:
        return assumed_credentials


def get_organizations_metadata(
    metadata_account: str, assumed_credentials: dict
) -> AWS_Organizations_Info:
    try:
        organizations_client = client(
            "organizations",
            aws_access_key_id=assumed_credentials["Credentials"]["AccessKeyId"],
            aws_secret_access_key=assumed_credentials["Credentials"]["SecretAccessKey"],
            aws_session_token=assumed_credentials["Credentials"]["SessionToken"],
        )
        organizations_metadata = organizations_client.describe_account(
            AccountId=metadata_account
        )
        list_tags_for_resource = organizations_client.list_tags_for_resource(
            ResourceId=metadata_account
        )
    except Exception as error:
        logger.critical(f"{error.__class__.__name__} -- {error}")
        sys.exit()
    else:
        # Convert Tags dictionary to String
        account_details_tags = ""
        for tag in list_tags_for_resource["Tags"]:
            account_details_tags += tag["Key"] + ":" + tag["Value"] + ","
        organizations_info = AWS_Organizations_Info(
            account_details_email=organizations_metadata["Account"]["Email"],
            account_details_name=organizations_metadata["Account"]["Name"],
            account_details_arn=organizations_metadata["Account"]["Arn"],
            account_details_org=organizations_metadata["Account"]["Arn"].split("/")[1],
            account_details_tags=account_details_tags,
        )
        return organizations_info


def generate_regional_clients(service, audit_info):
    regional_clients = []
    # Get json locally
    f = open_file(aws_services_json_file)
    data = parse_json_file(f)
    json_regions = data["services"][service]["regions"][audit_info.audited_partition]
    if audit_info.audited_regions:  # Check for input aws audit_info.audited_regions
        regions = list(
            set(json_regions).intersection(audit_info.audited_regions)
        )  # Get common regions between input and json
    else:  # Get all regions from json of the service and partition
        regions = json_regions
    for region in regions:
        regional_client = audit_info.audit_session.client(service, region_name=region)
        regional_client.region = region
        regional_clients.append(regional_client)

    return regional_clients


def send_to_security_hub(
    region: str, finding_output: Check_Output_JSON_ASFF, session: session.Session
):
    try:
        logger.info("Sending findings to Security Hub.")
        # Check if security hub is enabled in current region
        security_hub_client = session.client("securityhub", region_name=region)
        security_hub_client.describe_hub()

        # Check if Prowler integration is enabled in Security Hub
        if "prowler/prowler" not in str(
            security_hub_client.list_enabled_products_for_import()
        ):
            logger.error(
                f"Security Hub is enabled in {region} but Prowler integration does not accept findings. More info: https://github.com/prowler-cloud/prowler/#security-hub-integration"
            )

        # Send finding to Security Hub
        batch_import = security_hub_client.batch_import_findings(
            Findings=[finding_output.dict()]
        )
        if batch_import["FailedCount"] > 0:
            failed_import = batch_import["FailedFindings"][0]
            logger.error(
                f"Failed to send archived findings to AWS Security Hub -- {failed_import['ErrorCode']} -- {failed_import['ErrorMessage']}"
            )

    except Exception as error:
        logger.error(f"{error.__class__.__name__} -- {error} in region {region}")


# Move previous Security Hub check findings to ARCHIVED (as prowler didn't re-detect them)
def resolve_security_hub_previous_findings(
    output_directory: str, audit_info: AWS_Audit_Info
) -> list:
    logger.info("Checking previous findings in Security Hub to archive them.")
    # Read current findings from json-asff file
    with open(
        f"{output_directory}/prowler-output-{audit_info.audited_account}-{json_asff_file_suffix}"
    ) as f:
        json_asff_file = json.load(f)

    # Sort by region
    json_asff_file = sorted(json_asff_file, key=itemgetter("ProductArn"))
    # Group by region
    for product_arn, current_findings in groupby(
        json_asff_file, key=itemgetter("ProductArn")
    ):
        region = product_arn.split(":")[3]
        try:
            # Check if security hub is enabled in current region
            security_hub_client = audit_info.audit_session.client(
                "securityhub", region_name=region
            )
            security_hub_client.describe_hub()
            # Get current findings IDs
            current_findings_ids = []
            for finding in current_findings:
                current_findings_ids.append(finding["Id"])
            # Get findings of that region
            security_hub_client = audit_info.audit_session.client(
                "securityhub", region_name=region
            )
            findings_filter = {
                "ProductName": [{"Value": "Prowler", "Comparison": "EQUALS"}],
                "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                "AwsAccountId": [
                    {"Value": audit_info.audited_account, "Comparison": "EQUALS"}
                ],
                "Region": [{"Value": region, "Comparison": "EQUALS"}],
            }
            get_findings_paginator = security_hub_client.get_paginator("get_findings")
            findings_to_archive = []
            for page in get_findings_paginator.paginate(Filters=findings_filter):
                # Archive findings that have not appear in this execution
                for finding in page["Findings"]:
                    if finding["Id"] not in current_findings_ids:
                        finding["RecordState"] = "ARCHIVED"
                        finding["UpdatedAt"] = timestamp_utc.strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )

                        findings_to_archive.append(finding)
            logger.info(f"Archiving {len(findings_to_archive)} findings.")
            # Send archive findings to SHub
            list_chunked = [
                findings_to_archive[i : i + 100]
                for i in range(0, len(findings_to_archive), 100)
            ]
            for findings in list_chunked:
                batch_import = security_hub_client.batch_import_findings(
                    Findings=findings
                )
                if batch_import["FailedCount"] > 0:
                    failed_import = batch_import["FailedFindings"][0]
                    logger.error(
                        f"Failed to send archived findings to AWS Security Hub -- {failed_import['ErrorCode']} -- {failed_import['ErrorMessage']}"
                    )
        except Exception as error:
            logger.error(f"{error.__class__.__name__} -- {error} in region {region}")