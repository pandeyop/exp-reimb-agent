export const EXPENSE_STATUS = {
  PROCESSING: { label: 'Processing', icon: '◌', tone: 'neutral' },
  COMPLETE: { label: 'Ready', icon: '✓', tone: 'ok' },
  WARNING: { label: 'Ready · warning', icon: '✓', tone: 'warn-soft' },
  NEEDS_INFORMATION: { label: 'Needs info', icon: '?', tone: 'info' },
  POLICY_VIOLATION: { label: 'Over policy', icon: '⚠', tone: 'warn' },
  POSSIBLE_DUPLICATE: { label: 'Possible duplicate', icon: '⊘', tone: 'dup' },
  EXCLUDED: { label: 'Excluded', icon: '–', tone: 'muted' },
}

export const DOC_STATUS = {
  UPLOADED: { label: 'Uploaded', tone: 'neutral' },
  PROCESSING: { label: 'Processing', tone: 'neutral' },
  PROCESSED: { label: 'Processed', tone: 'ok' },
  NEEDS_ATTENTION: { label: 'Needs attention', tone: 'info' },
  FAILED: { label: 'Failed', tone: 'bad' },
}

export const CLAIM_STATUS = {
  DRAFT: { label: 'Draft', tone: 'neutral' },
  PROCESSING: { label: 'Processing', tone: 'neutral' },
  ACTION_REQUIRED: { label: 'Action required', tone: 'warn' },
  READY_TO_SUBMIT: { label: 'Ready to submit', tone: 'ok' },
  SUBMITTING: { label: 'Submitting…', tone: 'neutral' },
  SUBMITTED: { label: 'Submitted', tone: 'ok' },
}

export const SOURCE_LABEL = { extracted: 'Read', inferred: 'Inferred', user: 'You', classified: 'AI' }

export const PIPELINE = [
  ['document_uploaded', 'Ingest'],
  ['document_parsed', 'Understand'],
  ['expense_extracted', 'Extract'],
  ['classification_completed', 'Classify'],
  ['policy_check_completed', 'Policy'],
  ['duplicate_check_completed', 'Duplicates'],
  ['clarification_required', 'Gaps'],
  ['claim_ready', 'Ready'],
]
