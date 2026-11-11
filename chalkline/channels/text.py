import re


MEDIA_DIRECTIVE = re.compile(r"MEDIA:|\[\[(?:audio_as_voice|as_document)\]\]", re.IGNORECASE)
APPROVAL_COMMAND = re.compile(r"\b(?:APPROVE|REJECT)\s+A[0-9]+\b", re.IGNORECASE)


def customer_text(value):
    """Keep customer wording visible without executable transport or approval syntax."""
    text = APPROVAL_COMMAND.sub("[approval code omitted]", str(value))
    return MEDIA_DIRECTIVE.sub(
        lambda match: match[0].replace(":", chr(0x2236)).replace("[", chr(0xff3b)).replace("]", chr(0xff3d)),
        text,
    )
