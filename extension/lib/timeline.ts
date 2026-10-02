import type { RelatedDriveFile, TimelineItem, TimelineSnapshot } from './types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isRelatedDriveFile(value: unknown): value is RelatedDriveFile {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.name === 'string'
    && typeof value.mime_type === 'string'
    && Array.isArray(value.matched_terms)
    && typeof value.relevance_score === 'number';
}

function isTimelineItem(value: unknown): value is TimelineItem {
  if (!isRecord(value)
    || typeof value.id !== 'string'
    || !Number.isInteger(value.sequence)
    || typeof value.created_at !== 'string') return false;
  if (value.kind === 'task_suggestion') {
    return typeof value.title === 'string'
      && value.title.trim().length > 0
      && typeof value.description === 'string'
      && value.description.trim().length > 0
      && typeof value.confidence === 'number';
  }
  if (value.kind === 'search_results') {
    return isRecord(value.source)
      && typeof value.source.id === 'string'
      && typeof value.source.label === 'string'
      && typeof value.query === 'string'
      && Array.isArray(value.files)
      && value.files.every(isRelatedDriveFile)
      && (value.error === null || typeof value.error === 'string');
  }
  return false;
}

export function isTimelineSnapshot(value: unknown): value is TimelineSnapshot {
  if (!isRecord(value)
    || value.type !== 'timeline_snapshot'
    || typeof value.session_id !== 'string'
    || !Number.isInteger(value.revision)
    || !Array.isArray(value.items)
    || !value.items.every(isTimelineItem)) return false;
  const items = value.items as TimelineItem[];
  return items.every((item, index) => index === 0 || item.sequence < items[index - 1].sequence);
}

export function visibleTimelineItems(snapshot: TimelineSnapshot | undefined): TimelineItem[] {
  return snapshot?.items.filter((item) => item.kind !== 'search_results' || item.files.length > 0) ?? [];
}
