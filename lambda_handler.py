"""AWS Lambda entry point for the validation step.

Deploy the `intake/` package with this file, set the handler to
`lambda_handler.handler`, then run the worker with:
  VALIDATION_BACKEND=lambda VALIDATION_LAMBDA_NAME=<function-name>

The Lambda runs exactly the same rules as local mode (intake/validation.py),
so behaviour doesn't drift between environments.
"""
from intake.models import ValidateInput
from intake.validation import validate


def handler(event, context):
    data = ValidateInput.model_validate(event)
    return validate(data.referral, data.extracted).model_dump(mode="json")
