# Live WhatsApp validation

Confirmed 11 November 2026 on Windows, Hermes v0.22.2, using an isolated profile and three consenting WhatsApp accounts: business bot, owner and customer. Job text and quote details were fictional. Private account identifiers, pairing credentials and raw logs are excluded from the package.

| Check | Result |
| --- | --- |
| Owner command | Owner received the job list without creating an enquiry. |
| Customer text | Created one enquiry, retained the text, notified the owner and sent one approved acknowledgement. |
| Photo and native voice | Both attached to the existing job; stored files matched their ledger hashes. Voice was explicitly marked untranscribed. |
| Follow-up inside 12 hours | Captured on the same job; no second acknowledgement or customer agent turn. |
| Customer approval attempt | The exact pending approval command was captured as evidence; the approval remained unused and no PDF was sent. |
| Owner approval | Consumed the approval and sent the quote PDF; the customer confirmed receipt. |
| Repeated owner approval | No additional claim or PDF delivery. |
| Appointment reminder | Two successful executions of the real no-agent cron job produced one reminder, one claim and one send; the customer confirmed exactly one receipt. |

The reminder test was accelerated with the public Hermes cron-run command. It exercised the installed scheduler script and real WhatsApp transport, without waiting between two quarter-hour slots. Model-assisted drafting uses the host Hermes configuration; this record does not establish compatibility with every provider. Customer transcription, Cloud deployment and native Desktop activation were not verified in this live test.
