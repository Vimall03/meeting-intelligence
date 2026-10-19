# Meeting Intelligence extension

Live assistance and optional email recaps for Google Meet. Version 0.1.12 adds a Zoom browser preview for Chrome and Brave; the Zoom desktop app is not supported.

[Chrome Web Store](https://chromewebstore.google.com/detail/meeting-intelligence/ifednlgahhhflcbaojjhfphiockifodk) · [Product and preview download](https://meetingintelligence.auriqis.com/beta.html)

## Using the preview

- Join Google Meet or join Zoom **in your browser**, rather than opening the Zoom desktop app. In Zoom, connect computer audio so other participants can be heard.
- Click Meeting Intelligence in the browser toolbar while your meeting tab is selected. Allow microphone access, then choose **Start listening**. Let participants know and obtain any required consent first.
- Choose live assistance, an email recap, or both in Settings. Recaps stay off until you enable them.
- Stop listening releases your microphone immediately. Rejoining the same room within ten minutes can continue the same recap. Choose **Finish & email recap** when finished.
- Jira suggestions become tasks only after you review and confirm them.

The Web Store release and manual preview may have different versions. Store installations update through the store. For a manual update, stop listening, replace the files in the same extracted folder, then reload the existing extension entry. Keep that folder and entry to preserve sign-in and microphone setup.

## Development

Configure the endpoints using `.env.example`, then run:

```sh
npm ci
npm run typecheck
npm run test:session
npm run package
```

Load `.output/chrome-mv3` as an unpacked extension. The fixed public manifest key preserves the published extension identity. Do not replace it.

The shared meeting adapter handles supported meeting pages, visible call controls, room identities, and script recovery. Zoom passcodes never become room identities. Google Meet room IDs remain compatible with earlier builds. The existing offscreen document still owns audio capture, bounded replay, and reconnection; closing the panel does not stop listening.

Use `lib/user-messages.ts` and `userError` for every error display, with stable support codes and safe participant-facing copy. Keep provider details out of the interface.

## Release verification

Automated checks cover room detection, waiting-room rejection, platform isolation, capture/rejoin behavior, microphone cleanup, sign-in, timeline contracts, and recap state. A live two-person Zoom call with remote audio, microphone audio, reconnect, and recap verification is still required before promoting Zoom support out of preview. The release remains labelled as a Zoom browser preview in the Web Store submission.
