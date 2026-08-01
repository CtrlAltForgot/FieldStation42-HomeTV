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
    m.currentProgram = 0
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
    m.guideTask.url = m.server + "/api/tv/guide?hours=6"
    m.guideTask.ObserveField("state", "onGuideTaskState")
    m.guideTask.ObserveField("response", "onGuideLoaded")
    m.guideTask.ObserveField("error", "onRequestError")
    m.guideTask.control = "run"
end sub

sub onGuideTaskState(event)
    if event.GetData() = "stop" and m.rows.Count() = 0 and m.guideTask.error = ""
        m.top.FindNode("status").text = "The guide request ended without data. Press * to verify the server address."
    end if
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
    m.top.SetFocus(true)
    onChannelFocused(invalid)
    renderGuideGrid()
end sub

sub onChannelFocused(event)
    index = m.currentChannel
    if event <> invalid then index = m.channels.itemFocused
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
    m.currentProgram = 0
    showProgram(0)
end sub

sub onProgramFocused(event)
    m.currentProgram = m.programs.itemFocused
    showProgram(m.programs.itemFocused)
end sub

function addGuideLabel(group as Object, value as String, x as Integer, y as Integer, width as Integer, color as String, font as String) as Object
    label = CreateObject("roSGNode", "Label")
    label.text = value
    label.translation = [x, y]
    label.width = width
    label.height = 64
    label.color = color
    label.font = font
    label.vertAlign = "center"
    label.ellipsizeOnBoundary = true
    group.AppendChild(label)
    return label
end function

sub renderGuideGrid()
    grid = m.top.FindNode("guideGrid")
    count = grid.GetChildCount()
    if count > 0 then grid.RemoveChildrenIndex(count, 0)
    if m.rows.Count() = 0 then return
    firstRow = m.currentChannel - 3
    if firstRow < 0 then firstRow = 0
    if firstRow > m.rows.Count() - 7 then firstRow = m.rows.Count() - 7
    if firstRow < 0 then firstRow = 0
    for visibleRow = 0 to 6
        rowIndex = firstRow + visibleRow
        if rowIndex >= m.rows.Count() then exit for
        rowData = m.rows[rowIndex]
        y = visibleRow * 84
        channelBg = CreateObject("roSGNode", "Rectangle")
        channelBg.translation = [0, y]
        channelBg.width = 255
        channelBg.height = 78
        channelBg.color = "#101D2A"
        grid.AppendChild(channelBg)
        addGuideLabel(grid, rowData.channel_number + "  " + rowData.channel_name, 16, y + 7, 225, "#FFFFFF", "font:SmallBoldSystemFont")
        programs = rowData.programs
        if programs = invalid or programs.Count() = 0
            addGuideLabel(grid, "No programming available", 280, y + 7, 1440, "#8195A7", "font:SmallSystemFont")
        else
            selectedProgram = 0
            if rowIndex = m.currentChannel then selectedProgram = m.currentProgram
            firstProgram = selectedProgram - 1
            if firstProgram < 0 then firstProgram = 0
            if firstProgram > programs.Count() - 5 then firstProgram = programs.Count() - 5
            if firstProgram < 0 then firstProgram = 0
            for column = 0 to 4
                programIndex = firstProgram + column
                if programIndex >= programs.Count() then exit for
                programData = programs[programIndex]
                x = 270 + (column * 298)
                card = CreateObject("roSGNode", "Rectangle")
                card.translation = [x, y]
                card.width = 290
                card.height = 78
                card.color = "#202D3A"
                if rowIndex = m.currentChannel and programIndex = m.currentProgram then card.color = "#1675B8"
                grid.AppendChild(card)
                title = programData.display_title
                if title = invalid or title = "" then title = programData.title
                addGuideLabel(grid, title, x + 14, y + 2, 262, "#FFFFFF", "font:SmallBoldSystemFont")
                detail = programData.program_details
                if detail = invalid then detail = ""
                detailLabel = addGuideLabel(grid, detail, x + 14, y + 37, 262, "#AAB9C7", "font:TinySystemFont")
                detailLabel.height = 35
            end for
        end if
    end for
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
    art = program.artwork_url
    if art <> invalid and art <> "" then m.top.FindNode("artwork").uri = m.server + art
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
    if m.rows.Count() = 0
        if key = "options" then showServerDialog()
        return true
    end if
    if key = "up"
        m.currentChannel = m.currentChannel - 1
        if m.currentChannel < 0 then m.currentChannel = 0
        m.currentProgram = 0
        onChannelFocused(invalid)
        renderGuideGrid()
        return true
    else if key = "down"
        m.currentChannel = m.currentChannel + 1
        if m.currentChannel >= m.rows.Count() then m.currentChannel = m.rows.Count() - 1
        m.currentProgram = 0
        onChannelFocused(invalid)
        renderGuideGrid()
        return true
    else if key = "right"
        programs = m.rows[m.currentChannel].programs
        if programs <> invalid and m.currentProgram < programs.Count() - 1 then m.currentProgram = m.currentProgram + 1
        showProgram(m.currentProgram)
        renderGuideGrid()
        return true
    else if key = "left"
        if m.currentProgram > 0 then m.currentProgram = m.currentProgram - 1
        showProgram(m.currentProgram)
        renderGuideGrid()
        return true
    else if key = "OK"
        tuneCurrentChannel()
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
