# myHomeTV television clients

Both clients are couch-first front ends for the same myHomeTV server. Each
device creates and owns its own playback-session lease, so one television can
watch channel 2 while another watches channel 20. Viewers on the same channel
share a server broadcast without sharing navigation state.

## Roku

The `roku` directory is a SceneGraph channel. Create a zip whose root contains
`manifest`, `source`, `components`, and `images`, then upload it from the Roku
Development Application Installer. The app defaults to
`http://192.168.1.254:4243`; press `*` from the guide to change and save the
server address with the Roku on-screen keyboard.

After deploying the matching server release, the validated package is also
available at `http://YOUR-SERVER:4243/api/tv/apps/roku`.

The physical remote is sufficient: arrows browse, OK tunes, Back always exits
video to the guide, and channel Up/Down changes stations while watching. The
Video node has its own UI disabled, so an upstream web player cannot trap the
remote.

## LG webOS

The `webos` directory is a webOS web application. Package it with LG's webOS
CLI (`ares-package clients/webos`) and install the resulting IPK on a TV in
Developer Mode. The green remote key opens server settings. Back always exits
playback to the guide; channel Up/Down changes stations.

The validated IPK is available from the server at
`http://YOUR-SERVER:4243/api/tv/apps/webos`.

## Capacity

`FS42_HLS_MAX_SESSIONS` controls the maximum number of *different transcoded
channel broadcasts* (default `4`). Any number of viewers may lease an already
running channel within normal server/network limits. Increase the value only
when the Unraid CPU/GPU can encode that many distinct channels simultaneously.
