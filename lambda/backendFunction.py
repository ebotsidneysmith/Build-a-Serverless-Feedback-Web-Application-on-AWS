import json
import boto3
import base64
import os
import uuid
import logging
import html

from datetime import datetime, timezone
from botocore.exceptions import ClientError, BotoCoreError


# ============================================================
# CONFIGURATION
# ============================================================

TABLE_NAME = os.environ.get(
    "TABLE_NAME",
    "backend-dynamodb-table"
)

BUCKET_NAME = os.environ.get(
    "BUCKET_NAME",
    "backend-s3-bucket777"
)

ADMIN_EMAIL = os.environ.get(
    "ADMIN_EMAIL",
    "ebotsmith@gmail.com"
)

REGION = os.environ.get(
    "REGION",
    "us-east-2"
)


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ============================================================
# AWS CLIENTS
# ============================================================

dynamodb = boto3.resource(
    "dynamodb",
    region_name=REGION
)

s3 = boto3.client(
    "s3",
    region_name=REGION
)

ses = boto3.client(
    "ses",
    region_name=REGION
)

table = dynamodb.Table(TABLE_NAME)


# ============================================================
# RESPONSE HELPER
# ============================================================

def response(status_code, message, extra=None):

    result = {
        "message": message
    }

    if extra:
        result.update(extra)

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "POST,OPTIONS"
        },
        "body": json.dumps(result)
    }


# ============================================================
# REQUEST BODY PARSER
# ============================================================

def parse_request_body(event):

    raw_body = event.get("body")

    if raw_body is None:

        # Support direct Lambda test events
        if "name" in event and "email" in event:
            return event

        raise ValueError("Request body is missing.")

    if event.get("isBase64Encoded"):

        raw_body = base64.b64decode(raw_body).decode("utf-8")

    if isinstance(raw_body, dict):
        return raw_body

    return json.loads(raw_body)


# ============================================================
# PDF UPLOAD TO S3
# ============================================================

def upload_pdf(file_base64, feedback_id):

    if not file_base64:
        return None

    # Remove the data URI prefix if present
    encoded_file = file_base64.split(",")[-1]

    # Decode PDF
    pdf_data = base64.b64decode(
        encoded_file,
        validate=True
    )

    # Basic PDF signature validation
    if not pdf_data.startswith(b"%PDF-"):
        raise ValueError("The uploaded attachment is not a valid PDF.")

    # Generate unique S3 object key
    key = f"{feedback_id}.pdf"

    # Upload PDF to S3
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=pdf_data,
        ContentType="application/pdf"
    )

    logger.info("PDF successfully uploaded to S3: %s", key)

    # Generate a presigned URL valid for 24 hours
    file_url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": key
        },
        ExpiresIn=86400
    )

    return file_url


# ============================================================
# SAVE FEEDBACK TO DYNAMODB
# ============================================================

def save_feedback(
    feedback_id,
    name,
    email,
    message,
    file_url
):

    item = {
        # IMPORTANT:
        # This must match your DynamoDB partition key.
        "backend_id": feedback_id,

        "name": name,
        "email": email,
        "message": message,
        "file_url": file_url,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    table.put_item(
        Item=item,
        ConditionExpression="attribute_not_exists(backend_id)"
    )

    logger.info(
        "Feedback successfully saved to DynamoDB: %s",
        feedback_id
    )


# ============================================================
# SEND EMAIL NOTIFICATION USING SES
# ============================================================

def send_admin_email(
    feedback_id,
    name,
    email,
    message,
    file_url
):

    safe_name = html.escape(name)
    safe_email = html.escape(email)
    safe_message = html.escape(message).replace("\n", "<br>")

    attachment_html = ""

    if file_url:

        attachment_html = f"""
        <tr>
            <td><strong>Attachment</strong></td>
            <td>
                <a href="{html.escape(file_url, quote=True)}">
                    View PDF Attachment
                </a>
                <p>This link expires after 24 hours.</p>
            </td>
        </tr>
        """

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #f1f5f9;
                padding: 30px;
            }}

            .container {{
                max-width: 600px;
                margin: auto;
                background-color: #ffffff;
                border-radius: 10px;
                padding: 30px;
            }}

            h2 {{
                color: #1d4ed8;
            }}

            table {{
                width: 100%;
                border-collapse: collapse;
            }}

            td {{
                padding: 12px;
                border-bottom: 1px solid #e2e8f0;
                vertical-align: top;
            }}

            .footer {{
                margin-top: 25px;
                color: #64748b;
                font-size: 12px;
            }}
        </style>
    </head>

    <body>
        <div class="container">

            <h2>New Feedback Received</h2>

            <table>

                <tr>
                    <td><strong>Feedback ID</strong></td>
                    <td>{html.escape(feedback_id)}</td>
                </tr>

                <tr>
                    <td><strong>Name</strong></td>
                    <td>{safe_name}</td>
                </tr>

                <tr>
                    <td><strong>Email</strong></td>
                    <td>{safe_email}</td>
                </tr>

                <tr>
                    <td><strong>Message</strong></td>
                    <td>{safe_message}</td>
                </tr>

                {attachment_html}

            </table>

            <div class="footer">
                This email was automatically sent
                from your feedback application.
            </div>

        </div>
    </body>
    </html>
    """

    text_body = f"""
    New Feedback Received

    Feedback ID: {feedback_id}
    Name: {name}
    Email: {email}
    Message: {message}

    PDF Attachment: {file_url or "No attachment"}
    """

    ses.send_email(
        Source=ADMIN_EMAIL,

        Destination={
            "ToAddresses": [
                ADMIN_EMAIL
            ]
        },

        Message={
            "Subject": {
                "Data": "New Feedback Received",
                "Charset": "UTF-8"
            },

            "Body": {
                "Text": {
                    "Data": text_body,
                    "Charset": "UTF-8"
                },

                "Html": {
                    "Data": html_body,
                    "Charset": "UTF-8"
                }
            }
        }
    )

    logger.info("Feedback notification email sent.")


# ============================================================
# MAIN LAMBDA HANDLER
# ============================================================

def lambda_handler(event, context):

    logger.info("Feedback Lambda function started.")

    # --------------------------------------------------------
    # 1. Handle CORS preflight requests
    # --------------------------------------------------------

    request_context = event.get("requestContext", {})

    http_method = (
        event.get("httpMethod")
        or request_context.get("http", {}).get("method")
        or ""
    ).upper()

    if http_method == "OPTIONS":

        return response(
            200,
            "CORS preflight request successful."
        )

    try:

        # ----------------------------------------------------
        # 2. Parse incoming request
        # ----------------------------------------------------

        body = parse_request_body(event)

        # ----------------------------------------------------
        # 3. Extract feedback form fields
        # ----------------------------------------------------

        name = str(body.get("name") or "").strip()
        email = str(body.get("email") or "").strip()
        message = str(body.get("message") or "").strip()
        file_base64 = body.get("file_base64")

        # ----------------------------------------------------
        # 4. Validate required fields
        # ----------------------------------------------------

        if not name or not email or not message:
            return response(
                400,
                "Name, email, and message are required."
            )

        # ----------------------------------------------------
        # 5. Generate unique feedback ID
        # ----------------------------------------------------

        feedback_id = str(uuid.uuid4())
        logger.info(
            "Processing feedback ID: %s",
            feedback_id
        )

        # ----------------------------------------------------
        # 6. Upload PDF attachment to S3
        # ----------------------------------------------------

        file_url = upload_pdf(
            file_base64,
            feedback_id
        )

        # ----------------------------------------------------
        # 7. Save feedback to DynamoDB
        # ----------------------------------------------------

        save_feedback(
            feedback_id,
            name,
            email,
            message,
            file_url
        )

        # ----------------------------------------------------
        # 8. Send notification email using SES
        # ----------------------------------------------------

        email_sent = True

        try:

            send_admin_email(
                feedback_id,
                name,
                email,
                message,
                file_url
            )

        except (ClientError, BotoCoreError) as email_error:

            email_sent = False

            # Log email error without failing saved feedback
            logger.exception(
                "Feedback saved, but SES email failed: %s",
                email_error
            )

        # ----------------------------------------------------
        # 9. Return successful response
        # ----------------------------------------------------

        return response(
            200,
            "Feedback submitted successfully.",
            {
                "backend_id": feedback_id,
                "email_notification_sent": email_sent
            }
        )

    except ValueError as error:

        logger.warning(
            "Invalid feedback request: %s",
            error
        )

        return response(
            400,
            str(error)
        )

    except (ClientError, BotoCoreError) as error:

        logger.exception(
            "AWS service operation failed."
        )

        return response(
            500,
            "An AWS service operation failed. Check the Lambda logs."
        )

    except Exception:

        logger.exception(
            "Unexpected Lambda error."
        )

        return response(
            500,
            "Internal server error. Check the Lambda logs."
        )
