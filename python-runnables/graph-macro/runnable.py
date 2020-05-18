"""
The macro checks which DSS groups are available in Azure Active Directory
The users of those AAD groups are then synchronised with DSS users of type "LOCAL_NO_AUTH"
"""
import adal
import datetime
import pandas as pd
import requests

import dataiku
from dataiku.runnables import Runnable, ResultTable


class MyRunnable(Runnable):
    """The base interface for a Python runnable"""

    # Relevant URLs
    authority_url = "https://login.microsoftonline.com/"
    graph_url = "https://graph.microsoft.com/"
    graph_group_url = (
        "https://graph.microsoft.com/v1.0/groups?$filter=displayName eq '{}'&$select=id"
    )
    graph_members_url = "https://graph.microsoft.com/v1.0/groups/{}/members?$select=displayName,userPrincipalName"

    # License types. These should be ordered from most to least potent.
    possible_licenses = ["DATA_SCIENTIST", "DATA_ANALYST", "READER", "EXPLORER", "NONE"]

    # Define a translation dict that specifies how each credential should
    # be named in the user's secrets
    credentials_labels = {
        "graph_tenant_id": "Tenant ID",
        "graph_app_id": "Application ID",
        "graph_app_secret": "App secret",
        "graph_app_cert": "App certificate",
        "graph_app_cert_thumb": "App certificate thumbprint",
        "graph_user": "User principal",
        "graph_user_pwd": "User password",
    }

    def __init__(self, project_key, config, plugin_config):
        """
        Initialize the macro.

        :param project_key: the project in which the runnable executes
        :param config: the dict of the configuration of the object
        :param plugin_config: contains the plugin settings
        """

        # Assign input to self
        self.project_key = project_key
        self.config = config
        self.plugin_config = plugin_config
        self.flag_simulate = config["flag_simulate"]

        # Read the group configuration data from DSS
        if not config.get("groups_dataset"):
            raise Exception("No groups dataset has been selected.")
        groups_dataset_handle = dataiku.Dataset(config["groups_dataset"], project_key)
        self.groups_df = groups_dataset_handle.get_dataframe()

        self.client = dataiku.api_client()
        self.run_user = self.client.get_auth_info()["authIdentifier"]
        self.session = requests.Session()

        # Initialize a dataframe that will contain log data
        self.log_df = pd.DataFrame(columns=["date", "user", "type", "message"])

        # Configure auth method
        self.required_credentials = self.get_required_credentials(
            self.config["auth_method"]
        )

        # Read credentials
        if self.config["flag_user_credentials"]:
            self.credentials = self.get_credentials("user")
        else:
            self.credentials = self.get_credentials("parameters")

    def get_progress_target(self):
        """
        This defines the progress target, the highest value that progress_callback can return.
        Since the macro contains four steps, the target is four.
        """
        return 4, "NONE"

    # -------------------------------------------------------------------
    # Basic methods for translating
    # -------------------------------------------------------------------

    @staticmethod
    def get_user_id(email):
        """
        Creates a user ID based on an email address.

        :param email: the email address
        """
        return email.replace("@", "_")

    @staticmethod
    def list_diff(list1, list2):
        """Return elements of list1 that are not present in list2."""
        return list(set(list1) - set(list2))

    def get_license(self, license_list):
        """
        Given an list of license types, return the most potent license.

        :param license_list: a list with licenses
        """
        # For each license type, going from most to least potent, see if it is present in the list.
        # If so, return it as the assigned license type.
        for license_type in self.possible_licenses:
            if license_type in license_list:
                return license_type
        # If no match was found above, default to no license
        return "NONE"

    # -------------------------------------------------------------------
    # Methods related to input handling & validation
    # -------------------------------------------------------------------

    @staticmethod
    def get_required_credentials(auth_method):
        """Determine which credentials are required, based on the authentication method.

        :param auth_method: the selected authentication method
        """
        required_credentials = ["graph_tenant_id", "graph_app_id"]

        if auth_method == "auth_app_token":
            required_credentials.extend(["graph_app_secret"])
        elif auth_method == "auth_app_cert":
            required_credentials.extend(["graph_app_cert", "graph_app_cert_thumb"])
        elif auth_method == "auth_user_pwd":
            required_credentials.extend(["graph_user", "graph_user_pwd"])
        return required_credentials

    def validate_groups_df(self):
        """Verifies that the groups data contains the correct columns and license types."""
        mandatory_columns = ["dss_name", "aad_name", "license"]

        # Validate existence of correct columns
        column_names = list(self.groups_df.columns)
        if not column_names == mandatory_columns:
            raise Exception(
                "The groups dataset is not correctly configured."
                + " It should contain these columns: "
                + str(mandatory_columns)
            )
        # Validate content of license column
        license_values = list(self.groups_df["license"].unique())
        impossible_licenses = self.list_diff(license_values, self.possible_licenses)
        if impossible_licenses:
            raise Exception(
                f"Invalid license types were found in the groups configuration: {impossible_licenses}"
                f". Valid license values are: {self.possible_licenses}"
            )

    def get_credentials(self, source):
        """
        Returns a dictionary containing credentials for ADAL call to MS Graph.

        :param source: where the credentials are taken from, either 'user' or 'parameters'
        """
        # Empty list for missing credentials
        missing_credentials = []
        # Dictionary for present credentials
        credentials = {}

        if source == "user":
            # Load secrets from user profile [{key: value} ...]
            user_secrets = self.client.get_auth_info(with_secrets=True)["secrets"]
            secrets_dict = {secret["key"]: secret["value"] for secret in user_secrets}
        else:
            secrets_dict = self.config
        # For each required credential, check whether it is present
        for key in self.required_credentials:
            label = self.credentials_labels[key]
            try:
                if source == "user":
                    credentials[key] = secrets_dict[label]
                else:  # source == "parameters":
                    credentials[key] = secrets_dict[key]
                if not credentials[key]:
                    raise KeyError
            except (KeyError, IndexError):
                missing_credentials.append(label)
        if missing_credentials:
            raise KeyError(f"Please specify these credentials: {missing_credentials}")
        return credentials

    # -------------------------------------------------------------------
    # Methods related to logging
    # -------------------------------------------------------------------

    def add_log(self, message, log_type="INFO"):
        """
        Add a record to the logging dataframe.

        :param message: The text to be logged
        :param log_type: The message type, 'INFO' by default.
        """
        new_log = {
            "date": str(datetime.datetime.now()),
            "user": self.run_user,
            "type": log_type,
            "message": message,
        }

        self.log_df = self.log_df.append(new_log, ignore_index=True)

    def clear_log(self):
        """
        Empties the log. Useful for testing.
        """
        self.log_df = pd.DataFrame(columns=["date", "user", "type", "message"])

    def save_log(self, dss_log_dataset_name):
        """
        Saves the log data to a DSS dataset.

        :param dss_log_dataset_name: The name of a DSS dataset
        """
        log_dataset = dataiku.Dataset(dss_log_dataset_name, self.project_key)
        log_dataset.write_with_schema(self.log_df)

    def create_resulttable(self):
        """
        Transforms the log dataframe into a ResultTable.
        """
        result_table = ResultTable()

        for column_name in self.log_df.keys():
            result_table.add_column(column_name, str.capitalize(column_name), "STRING")
        for log_row in self.log_df.itertuples():
            result_table.add_record(list(log_row)[1:])
        return result_table

    # -------------------------------------------------------------------
    # Methods that interact with the Graph API
    # -------------------------------------------------------------------

    def set_session_headers(self):
        """
        Starts an ADAL session with Microsoft Graph.
        """
        auth_context = adal.AuthenticationContext(
            self.authority_url + self.credentials["graph_tenant_id"], api_version=None
        )

        if self.config["auth_method"] == "auth_app_token":
            token_response = auth_context.acquire_token_with_client_credentials(
                self.graph_url,
                self.credentials["graph_app_id"],
                self.credentials["graph_app_secret"],
            )
        elif self.config["auth_method"] == "auth_app_cert":
            token_response = auth_context.acquire_token_with_client_certificate(
                self.graph_url,
                self.credentials["graph_app_id"],
                self.credentials["graph_app_cert"],
                self.credentials["graph_app_cert_thumb"],
            )
        elif self.config["auth_method"] == "auth_user_pwd":
            token_response = auth_context.acquire_token_with_username_password(
                self.graph_url,
                self.credentials["graph_user"],
                self.credentials["graph_user_pwd"],
                self.credentials["graph_app_id"],
            )
        else:
            raise Exception(f"Invalid authentication method")
        self.session.headers.update(
            {"Authorization": f'Bearer {token_response["accessToken"]}'}
        )

    def query_group(self, group_name_aad):
        """
        AAD groups have a unique ID in Graph, which this function retrieves.

        :param group_name_aad: AAD group name
        :return: the Graph ID for the AAD group
        """
        print("ALX:group_name_aad={}".format(group_name_aad))#dss-user
        try:
            query_url = self.graph_group_url.format(group_name_aad)
            print("ALX:query_url={}".format(query_url))
            query_result = self.session.get(query_url)
            print("ALX:query_result={}".format(query_result.text))
            query_result = query_result.json()["value"]
            if query_result:
                return query_result[0]["id"]
            else:
                self.add_log(
                    f"No return value from Graph for group {group_name_aad}", "WARNING",
                )
        except Exception as e:
            self.add_log(
                f'Error calling Graph API for group "{group_name_aad}: {str(e)}',
                "WARNING",
            )

    def query_members(self, group_id, group_name_dss):
        """
        Send query to Graph for members of a group, by ID.

        :param group_id: the ID of a group in Graph
        :param group_name_dss: DSS group name, returned in result
        :return: a dataframe with 4 columns: display name, email, groups, login
        """
        print('ALX:group_id, group_name_dss={},{}'.format(group_id, group_name_dss))
        group_members = pd.DataFrame()

        try:
            query_url = self.graph_members_url.format(group_id)
            print('ALX:query_url={}'.format(query_url))

            while query_url:
                query_result = self.session.get(query_url)
                query_result = query_result.json()
                print("ALX:query_result={}".format(query_result))
                query_url = query_result.get("@odata.nextLink", "")
                group_members = group_members.append(
                    pd.DataFrame(query_result["value"]), ignore_index=True
                )
            # The first column is meaningless and is removed using iloc
            group_members = group_members.iloc[:, 1:]

            # Rename the columns
            group_members.columns = ["displayName", "email"]

            # Add two columns
            group_members["groups"] = group_name_dss
            group_members["login"] = group_members["email"].apply(self.get_user_id)

            return group_members
        except Exception as e:
            self.add_log(
                f'Group "{group_name_dss}" members cannot be retrieved from AAD: {str(e)}',
                "WARNING",
            )

    # -------------------------------------------------------------------
    # Methods that handle user creation, drop, and alteration within DSS
    # -------------------------------------------------------------------

    def user_create(self, user_id, display_name, email, groups, user_license):
        """
        Create a new DSS user.

        The parameters are taken from the parameters of dataiku.client.create_user.
        """
        if user_license == "NONE":
            self.add_log(
                f'User "{user_id}" will not be created, since he has no license.'
            )
            return
        if self.flag_simulate:
            self.add_log(
                f'User "{user_id}" will be created and assigned groups "{groups}"'
            )
            return
        # Create the user in DSS
        user = self.client.create_user(
            login=user_id,
            display_name=display_name,
            groups=list(groups),
            password="",
            source_type="LOCAL_NO_AUTH",
            profile=user_license,
        )

        # Request and alter the user definition to set the e-mail address
        user_def = user.get_definition()
        user_def["email"] = email
        user.set_definition(user_def)

        self.add_log(
            f'User "{user_id}" has been created and assigned groups "{groups}"'
        )

    def user_update(self, user_id, groups, user_license):
        """
        Update the group membership of a DSS user.

        :param user_id: the account name in DSS
        :param groups: a list of group memberships
        :param user_license: the license for this user
        """
        if self.flag_simulate:
            self.add_log(
                f'User "{user_id}" groups will be modified to "{groups}", user license "{user_license}"'
            )
            return
        # Request and alter the user's definition
        user = self.client.get_user(user_id)
        user_def = user.get_definition()
        user_def["groups"] = groups
        user_def["userProfile"] = user_license
        user.set_definition(user_def)

        self.add_log(
            f'User "{user_id}" groups have been modified to "{groups}", user license "{user_license}"'
        )

    def user_delete(self, user_id, reason):
        """
        Remove an user from DSS
        :param user_id: The user's login
        :param reason: reason for deletion, e.g. "No license" or "Not found in AAD"
        """
        if self.flag_simulate:
            self.add_log(f'User "{user_id}" will be deleted. Reason: {reason}')
            return
        user = self.client.get_user(user_id)
        user.delete()

        self.add_log(f'User "{user_id}" has been deleted. Reason: {reason}')

    # -------------------------------------------------------------------
    # The main run method
    # -------------------------------------------------------------------

    def run(self, progress_callback):
        """
        The main method of Macro runnable.

        :param progress_callback: standard parameter for DSS runnable
        """

        try:
            progress_callback(0)

            ############################
            # PHASE 1 - Validate DSS groups

            # Read the group configuration data from DSS
            self.validate_groups_df()

            # Read data about groups and users from DSS
            dss_users = pd.DataFrame(
                self.client.list_users(),
                columns=[
                    "login",
                    "displayName",
                    "email",
                    "groups",
                    "sourceType",
                    "userProfile",
                ],
            )

            dss_groups = [group["name"] for group in self.client.list_groups()]

            # Compare DSS groups with the groups in the input
            groups_from_input = list(self.groups_df["dss_name"])
            local_groups = self.list_diff(dss_groups, groups_from_input)
            missing_groups = self.list_diff(groups_from_input, dss_groups)

            if missing_groups:
                raise Exception(f"Groups {missing_groups} are missing from DSS")
            progress_callback(1)

            ############################
            # PHASE 2 - Query Graph

            # Connect to Graph API
            self.set_session_headers()

            # Init empty data frame
            group_members_df = pd.DataFrame()

            # Loop over each group and query the API
            print('ALX:before')
            for row in self.groups_df.itertuples():
                print("ALX:row={}".format(row)) #ALX:row=Pandas(Index=0, dss_name='dss-user', aad_name='dss-user', license='DATA_SCIENTIST')
                group_id = self.query_group(row.aad_name)
                print("ALX:group_id={}".format(group_id)) #is None
                if not group_id:
                    continue
                print("ALX:querying")
                group_members = self.query_members(group_id, row.dss_name)
                group_members_df = group_members_df.append(
                    group_members, ignore_index=True
                )
            progress_callback(2)

            ############################
            # PHASE 3 - Group data frame

            license_lookup = self.groups_df.iloc[:, [0, 2]]

            # Sort and group the data frame
            aad_users = (
                group_members_df.sort_values(by=["login", "groups"])
                .merge(license_lookup, left_on="groups", right_on="dss_name")
                .groupby(by=["login", "displayName", "email"])["groups", "license"]
                .agg(["unique"])
                .reset_index()
            )

            aad_users.columns = aad_users.columns.droplevel(1)

            progress_callback(3)

            ############################
            # PHASE 4 - Update DSS users

            # Create a comparison table between AAD and DSS
            user_comparison = aad_users.merge(
                dss_users,
                how="outer",
                on=["login", "displayName", "email"],
                suffixes=("_aad", "_dss"),
                indicator=True,
            )

            # Replace NaN with empty lists in the license column
            for row in user_comparison.loc[
                user_comparison.license.isnull(), "license"
            ].index:
                user_comparison.at[row, "license"] = []
            # Iterate over this table
            for _, row in user_comparison.iterrows():
                user_id = row["login"]
                user_license = self.get_license(row["license"])

                # The _merge column was created by the indicator parameter of pd.merge.
                # It holds data about which sources contain this row.
                source = row["_merge"]
                print('ALX:source={}, row={}'.format(source, row))

                # If user only exists in AAD, create the user.
                # The user_create function checks whether the user has a license.
                if source == "left_only":
                    self.user_create(
                        user_id=user_id,
                        display_name=row["displayName"],
                        email=row["email"],
                        groups=row["groups_aad"],
                        user_license=user_license,
                    )
                    continue
                # The user exists in DSS; store the DSS user type as a variable.
                dss_user_type = row["sourceType"]

                if source == "right_only":
                    # The user exists only in DSS as a LOCAL_NO_AUTH account: delete.
                    if dss_user_type == "LOCAL_NO_AUTH":
                        self.user_delete(user_id, "Not found in AAD.")
                    continue
                # The user exists in AAD, and in DSS as LOCAL or LDAP type.
                # This is strange, and it is logged as a warning.
                if dss_user_type != "LOCAL_NO_AUTH":
                    self.add_log(
                        f"User {user_id} has DSS user type {dss_user_type}, while "
                        f"LOCAL_NO_AUTH was expected",
                        "WARNING",
                    )
                    continue
                # The user exists in DSS, but its AAD memberships don't grant a license: delete.
                if user_license == "NONE":
                    self.user_delete(user_id, "No license.")
                    continue
                # Compare group memberships in DSS & AAD. If any discrepancies are found: update.
                users_local_groups = list(set(row["groups_dss"]) & set(local_groups))
                all_groups = list(row["groups_aad"])
                all_groups.extend(users_local_groups)

                if (
                    self.list_diff(all_groups, row["groups_dss"])
                    or user_license != row["userProfile"]
                ):
                    self.user_update(
                        user_id=user_id, groups=all_groups, user_license=user_license
                    )
            progress_callback(4)  # Phase 4 completed - macro has finished
        except Exception as e:
            self.add_log(str(e), "ERROR")
        finally:
            if self.config.get("log_dataset"):
                self.save_log(self.config["log_dataset"])
            return self.create_resulttable()