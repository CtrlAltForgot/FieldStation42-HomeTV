# Free live news

myHomeTV can install four zero-subscription live-news channels from the main
management page: ABC News Live, CBS News 24/7, NBC News NOW, and LiveNOW from
FOX. Select the publishers you want under **Free Live News**, then choose
**Add selected news channels**. Suggested channels are 20–23; an occupied
number is skipped automatically. Running the installer again updates the
managed station in place and preserves its assigned number.

These stations resolve direct HLS from each publisher's public official live
broadcast. myHomeTV does not record or redistribute the feed, and no paid API
key or subscription is required. The short-lived address is handed directly
to the viewer's native video player, eliminating the YouTube overlay and
keeping Roku, webOS, and browser controls inside myHomeTV. Publisher
availability, regional limits, advertisements, and programming remain outside
myHomeTV's control. The browser retains a non-interactive official embed only
as a last-resort fallback; television clients never use it.

Live-news stations appear alongside normal channels in the full guide and
watch-page channel selector. Their guide artwork is generated and cached by
the browser immediately, while selecting the current block opens the official
feed. They do not create a media catalog, SQLite schedule, HLS transcode, or
local recording. CBS News 24/7 uses the query-safe schedule API because the
slash in its name is channel data, not a URL path separator.

Only source IDs compiled into `fs42/live_news.py` can be installed through the
one-click endpoint. Channel IDs are validated and the server constructs the
embed URL; clients cannot submit an arbitrary URL. This keeps the installer
from becoming an open redirect or general-purpose proxy.

Normal myHomeTV channels continue to use local HLS. At item boundaries the
browser begins preparing the next item twenty seconds early and waits for a
real buffered media fragment before swapping players. This is particularly
important for back-to-back advertisements. FFmpeg decoder chatter below fatal
severity is suppressed in the live broadcaster; a failed process still exits
and triggers the existing recovery path.
