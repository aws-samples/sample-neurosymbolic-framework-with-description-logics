"""Tests for the CLI chatbot's friendly auth-error detection."""
from ontology_qa_cli.chatbot import _is_auth_error


class TestIsAuthError:
    def test_unrecognized_client_message(self):
        exc = Exception(
            "An error occurred (UnrecognizedClientException) when calling the "
            "Converse operation: The security token included in the request is invalid"
        )
        assert _is_auth_error(exc) is True

    def test_expired_token(self):
        exc = Exception("An error occurred (ExpiredTokenException): token expired")
        assert _is_auth_error(exc) is True

    def test_access_denied(self):
        exc = Exception("An error occurred (AccessDeniedException): not authorized")
        assert _is_auth_error(exc) is True

    def test_no_credentials_by_type_name(self):
        class NoCredentialsError(Exception):
            pass

        assert _is_auth_error(NoCredentialsError("Unable to locate credentials")) is True

    def test_botocore_clienterror_shape(self):
        # Real botocore ClientError, if available.
        try:
            from botocore.exceptions import ClientError
        except ImportError:  # pragma: no cover
            return
        err = ClientError(
            {"Error": {"Code": "UnrecognizedClientException", "Message": "bad token"}},
            "Converse",
        )
        assert _is_auth_error(err) is True

    def test_ordinary_program_error_is_not_auth(self):
        assert _is_auth_error(ValueError("entity not found in ontology")) is False

    def test_timeout_is_not_auth(self):
        assert _is_auth_error(TimeoutError("query program exceeded 120 seconds")) is False
