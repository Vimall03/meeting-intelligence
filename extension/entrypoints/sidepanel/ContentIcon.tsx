import { UiIcon } from './UiIcon';

const serviceAssets: Record<string, string> = {
  google_drive: '/icons/google-drive.png',
  atlassian: '/icons/atlassian.svg',
  jira: '/icons/jira.svg',
  confluence: '/icons/confluence.svg',
};

export function ServiceIcon({ service, size = 24 }: { service: string; size?: number }) {
  const src = Object.hasOwn(serviceAssets, service) ? serviceAssets[service] : undefined;
  return src
    ? <img className="service-icon" src={src} width={size} height={size} alt="" aria-hidden="true" draggable="false" />
    : <UiIcon name="folder" size={size} />;
}

// File decoration only; source routing and file labels stay in App.
export function FileIcon({ mimeType }: { mimeType: string }) {
  if (mimeType === 'application/vnd.atlassian.jira.issue') return <span className="file-icon is-service"><ServiceIcon service="jira" /></span>;
  if (mimeType === 'application/vnd.atlassian.confluence.page') return <span className="file-icon is-service"><ServiceIcon service="confluence" /></span>;
  const type = mimeType === 'application/vnd.google-apps.spreadsheet' ? 'spreadsheet'
    : mimeType === 'application/vnd.google-apps.presentation' ? 'presentation'
      : mimeType === 'application/vnd.google-apps.document' ? 'document'
        : mimeType === 'application/pdf' ? 'pdf'
          : mimeType.startsWith('image/') ? 'image'
            : mimeType.startsWith('video/') ? 'video'
              : mimeType.startsWith('audio/') ? 'audio' : 'file';
  return <span className={`file-icon file-icon--${type}`}><UiIcon name={type === 'pdf' ? 'document' : type} size={18} /></span>;
}
