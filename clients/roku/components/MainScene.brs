sub init()
    m.server = "http://192.168.1.254:4243"
    registry = CreateObject("roRegistrySection", "myHomeTV")
    if registry.Exists("server") then m.server = registry.Read("server")
    m.channels = m.top.FindNode("channels")
    m.programs = m.top.FindNode("programs")
    m.video = m.top.FindNode("video")
    m.channels.ObserveField("itemFocused", "onChannelFocused")
    m.channels.ObserveField("itemSelected", "onChannelSelected")
    m.programs.ObserveField("itemFocused", "onProgramFocused")
    m.programs.ObserveField("itemSelected", "onProgramSelected")
    m.video.ObserveField("state", "onVideoState")
    m.rows = []
    m.currentChannel = 0
    m.sessionId = ""
    m.inPlayer = false
    loadGuide()
    m.clockTimer = CreateObject("roSGNode", "Timer")
    m.clockTimer.duration = 1
    m.clockTimer.repeat = true
    m.clockTimer.ObserveField("fire", "updateClock")
    m.clockTimer.control = "start"
    updateClock()
end sub

sub loadGuide()
    m.top.FindNode("status").text = "Connecting to " + m.server + "…"
    m.guideTask = CreateObject("roSGNode", "RequestTask")
    m.guideTask.url = m.server + "/api/tv/guide?hours=12"
    m.guideTask.ObserveField("response", "onGuideLoaded")
    m.guideTask.ObserveField("error", "onRequestError")
    m.guideTask.control = "run"
end sub

sub onGuideLoaded(event)
    data = event.GetData()
    if data = invalid or data.channels = invalid then return
    m.rows = data.channels
    root = CreateObject("roSGNode", "ContentNode")
    for each row in m.rows
        item = root.CreateChild("ContentNode")
        item.title = row.channel_number + "   " + row.channel_name
    end for
    m.channels.content = root
    m.top.FindNode("status").text = "Ready"
    m.channels.SetFocus(true)
    onChannelFocused(invalid)
end sub

sub onChannelFocused(event)
    index = m.channels.itemFocused
    if index < 0 or index >= m.rows.Count() then return
    m.currentChannel = index
    row = m.rows[index]
    root = CreateObject("roSGNode", "ContentNode")
    if row.programs <> invalid
        for each program in row.programs
            item = root.CreateChild("ContentNode")
            title = program.display_title
            if title = invalid or title = "" then title = program.title
            details = program.program_details
            if details = invalid then details = ""
            item.title = title + "    " + details
        end for
    end if
    m.programs.content = root
    m.programs.jumpToItem = 0
    showProgram(0)
end sub

sub onProgramFocused(event)
    showProgram(m.programs.itemFocused)
end sub

sub showProgram(index as Integer)
    if m.currentChannel < 0 or m.currentChannel >= m.rows.Count() then return
    row = m.rows[m.currentChannel]
    if row.programs = invalid or index < 0 or index >= row.programs.Count() then
        m.top.FindNode("programTitle").text = row.channel_name
        m.top.FindNode("programDetails").text = "No programming available"
        return
    end if
    program = row.programs[index]
    title = program.display_title
    if title = invalid or title = "" then title = program.title
    m.top.FindNode("programTitle").text = title
    details = program.program_details
    if details = invalid then details = ""
    m.top.FindNode("programDetails").text = details
    description = ""
    if program.meta <> invalid
        if program.meta.plot <> invalid then description = program.meta.plot
        if description = "" and program.meta.description <> invalid then description = program.meta.description
    end if
    m.top.FindNode("programDescription").text = description
    m.top.FindNode("artwork").uri = m.server + row.artwork_url + "?at=" + program.start_time
end sub

sub onChannelSelected(event)
    tuneCurrentChannel()
end sub

sub onProgramSelected(event)
    tuneCurrentChannel()
end sub

sub tuneCurrentChannel()
    if m.rows.Count() = 0 then return
    stopPlayback()
    row = m.rows[m.currentChannel]
    m.top.FindNode("status").text = "Tuning channel " + row.channel_number + "…"
    m.playTask = CreateObject("roSGNode", "RequestTask")
    m.playTask.url = m.server + "/api/watch/sessions"
    m.playTask.method = "POST"
    m.playTask.body = FormatJson({channel: row.channel_number, profile: "auto", subtitles: "auto", client: "roku"})
    m.playTask.ObserveField("response", "onPlaybackReady")
    m.playTask.ObserveField("error", "onRequestError")
    m.playTask.control = "run"
end sub

sub onPlaybackReady(event)
    result = event.GetData()
    if result = invalid then return
    url = result.playlist_url
    if url = invalid then return
    if Left(url, 1) = "/" then url = m.server + url
    content = CreateObject("roSGNode", "ContentNode")
    content.url = url
    content.streamFormat = "hls"
    content.live = true
    if result.now <> invalid
        content.title = result.now.program_title
        m.top.FindNode("hudTitle").text = "CH " + result.now.channel_number + "  " + result.now.program_title
    end if
    m.sessionId = ""
    if result.session_id <> invalid then m.sessionId = result.session_id
    m.video.content = content
    m.video.visible = true
    m.top.FindNode("playerHud").visible = true
    m.inPlayer = true
    m.video.SetFocus(true)
    m.video.control = "play"
end sub

sub stopPlayback()
    if m.video <> invalid
        m.video.control = "stop"
        m.video.visible = false
    end if
    m.top.FindNode("playerHud").visible = false
    m.inPlayer = false
    if m.sessionId <> ""
        cleanup = CreateObject("roSGNode", "RequestTask")
        cleanup.url = m.server + "/api/watch/sessions/" + m.sessionId
        cleanup.method = "DELETE"
        cleanup.control = "run"
        m.sessionId = ""
    end if
end sub

sub onVideoState(event)
    state = event.GetData()
    if state = "error"
        m.top.FindNode("status").text = "Playback failed. Press Back for the guide."
        m.top.FindNode("playerHud").visible = true
    else if state = "finished" and m.inPlayer
        tuneCurrentChannel()
    end if
end sub

sub onRequestError(event)
    message = event.GetData()
    m.top.FindNode("status").text = message
    if m.inPlayer then m.top.FindNode("playerHud").visible = true
end sub

sub updateClock()
    now = CreateObject("roDateTime")
    now.ToLocalTime()
    hours = now.GetHours()
    suffix = "am"
    if hours >= 12 then suffix = "pm"
    displayHour = hours mod 12
    if displayHour = 0 then displayHour = 12
    minutes = now.GetMinutes().ToStr()
    if Len(minutes) = 1 then minutes = "0" + minutes
    m.top.FindNode("clock").text = displayHour.ToStr() + ":" + minutes + suffix
end sub

sub changeChannel(delta as Integer)
    nextIndex = m.currentChannel + delta
    if nextIndex < 0 then nextIndex = m.rows.Count() - 1
    if nextIndex >= m.rows.Count() then nextIndex = 0
    m.currentChannel = nextIndex
    m.channels.jumpToItem = nextIndex
    tuneCurrentChannel()
end sub

sub onLaunchChannel()
    if m.top.launchChannel = "" or m.rows.Count() = 0 then return
    for index = 0 to m.rows.Count() - 1
        if m.rows[index].channel_number = m.top.launchChannel
            m.currentChannel = index
            m.channels.jumpToItem = index
            tuneCurrentChannel()
            return
        end if
    end for
end sub

function onKeyEvent(key as String, press as Boolean) as Boolean
    if not press then return false
    if m.inPlayer
        if key = "back"
            stopPlayback()
            m.channels.SetFocus(true)
            return true
        else if key = "channelup" or key = "up"
            changeChannel(1)
            return true
        else if key = "channeldown" or key = "down"
            changeChannel(-1)
            return true
        else if key = "options"
            m.video.globalCaptionMode = "On"
            return true
        else
            return true
        end if
    end if
    if key = "right" and m.programs.content <> invalid
        m.programs.SetFocus(true)
        return true
    else if key = "left"
        m.channels.SetFocus(true)
        return true
    else if key = "options"
        showServerDialog()
        return true
    end if
    return false
end function

sub showServerDialog()
    dialog = CreateObject("roSGNode", "KeyboardDialog")
    dialog.title = "myHomeTV server address"
    dialog.text = m.server
    dialog.buttons = ["Save", "Cancel"]
    dialog.ObserveField("buttonSelected", "onServerDialog")
    m.top.dialog = dialog
end sub

sub onServerDialog()
    dialog = m.top.dialog
    if dialog.buttonSelected = 0
        value = dialog.text
        if Right(value, 1) = "/" then value = Left(value, Len(value) - 1)
        m.server = value
        registry = CreateObject("roRegistrySection", "myHomeTV")
        registry.Write("server", m.server)
        registry.Flush()
        loadGuide()
    end if
    dialog.close = true
end sub
