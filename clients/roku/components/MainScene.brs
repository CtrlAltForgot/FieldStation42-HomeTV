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
    m.timeline = []
    m.windowOffsetSeconds = 0
    m.windowSeconds = 10800
    m.trackWidth = 1490
    m.autoFollowNow = true
    m.guideLoadedSeconds = 0
    m.lastGuideShift = -1
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
    if data.timeline <> invalid then m.timeline = data.timeline
    if data.current_offset_second <> invalid then m.windowOffsetSeconds = data.current_offset_second
    m.guideLoadedSeconds = CreateObject("roDateTime").AsSeconds()
    root = CreateObject("roSGNode", "ContentNode")
    for each row in m.rows
        item = root.CreateChild("ContentNode")
        item.title = row.channel_number + "   " + row.channel_name
    end for
    m.channels.content = root
    m.top.FindNode("status").text = ""
    m.top.SetFocus(true)
    onChannelFocused(invalid)
    renderTimeline()
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

function closestProgramIndex(rowIndex as Integer, targetSecond as Integer) as Integer
    if rowIndex < 0 or rowIndex >= m.rows.Count() then return 0
    programs = m.rows[rowIndex].programs
    if programs = invalid or programs.Count() = 0 then return 0
    closest = 0
    best = 999999.0
    for index = 0 to programs.Count() - 1
        starts = programs[index].guide_start_second
        ends = programs[index].guide_end_second
        if starts <> invalid and ends <> invalid
            distance = 0.0
            if targetSecond < starts then distance = starts - targetSecond
            if targetSecond > ends then distance = targetSecond - ends
            if distance < best
                best = distance
                closest = index
            end if
        end if
    end for
    return closest
end function

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

function addMarqueeLabel(group as Object, value as String, x as Integer, y as Integer, width as Integer, height as Integer, color as String, font as String) as Object
    label = CreateObject("roSGNode", "ScrollingLabel")
    label.text = value
    label.translation = [x, y]
    label.maxWidth = width
    label.height = height
    label.color = color
    label.font = font
    label.horizAlign = "left"
    label.vertAlign = "center"
    label.scrollSpeed = 46
    label.repeatCount = -1
    group.AppendChild(label)
    return label
end function

sub renderTimeline()
    timeline = m.top.FindNode("timeline")
    count = timeline.GetChildCount()
    if count > 0 then timeline.RemoveChildrenIndex(count, 0)
    addGuideLabel(timeline, "NOW", 8, 0, 120, "#62D5FF", "font:SmallBoldSystemFont")
    for each tick in m.timeline
        second = tick.minute * 60
        if second >= m.windowOffsetSeconds and second <= m.windowOffsetSeconds + m.windowSeconds
            x = Int((second - m.windowOffsetSeconds) * m.trackWidth / m.windowSeconds)
            addGuideLabel(timeline, tick.label, x + 8, 0, 180, "#AAB9C7", "font:SmallSystemFont")
            marker = CreateObject("roSGNode", "Rectangle")
            marker.translation = [x, 35]
            marker.width = 1
            marker.height = 625
            marker.color = "#2B4154"
            timeline.AppendChild(marker)
        end if
    end for
end sub

sub ensureProgramVisible(programData as Object)
    starts = programData.guide_start_second
    ends = programData.guide_end_second
    if starts = invalid or ends = invalid then return
    changed = false
    if starts < m.windowOffsetSeconds
        m.windowOffsetSeconds = starts
        if m.windowOffsetSeconds < 0 then m.windowOffsetSeconds = 0
        changed = true
    else if ends > m.windowOffsetSeconds + m.windowSeconds
        m.windowOffsetSeconds = starts - 1800
        if m.windowOffsetSeconds < 0 then m.windowOffsetSeconds = 0
        changed = true
    end if
    if changed then renderTimeline()
end sub

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
        addMarqueeLabel(grid, rowData.channel_number + "  " + rowData.channel_name, 16, y + 7, 225, 64, "#FFFFFF", "font:SmallBoldSystemFont")
        programs = rowData.programs
        if programs = invalid or programs.Count() = 0
            addGuideLabel(grid, "No programming available", 280, y + 7, 1440, "#8195A7", "font:SmallSystemFont")
        else
            for programIndex = 0 to programs.Count() - 1
                programData = programs[programIndex]
                starts = programData.guide_start_second
                ends = programData.guide_end_second
                if starts <> invalid and ends <> invalid
                    visibleStart = starts
                    if visibleStart < m.windowOffsetSeconds then visibleStart = m.windowOffsetSeconds
                    visibleEnd = ends
                    if visibleEnd > m.windowOffsetSeconds + m.windowSeconds then visibleEnd = m.windowOffsetSeconds + m.windowSeconds
                    if visibleEnd > visibleStart
                        x = 270 + Int((visibleStart - m.windowOffsetSeconds) * m.trackWidth / m.windowSeconds)
                        width = Int((visibleEnd - visibleStart) * m.trackWidth / m.windowSeconds) - 6
                        if width < 72 then width = 72
                        if x + width > 1760 then width = 1760 - x
                        if width > 0
                            card = CreateObject("roSGNode", "Rectangle")
                            card.translation = [x, y]
                            card.width = width
                            card.height = 78
                            card.color = "#202D3A"
                            if rowIndex = m.currentChannel and programIndex = m.currentProgram then card.color = "#1675B8"
                            grid.AppendChild(card)
                            title = programData.display_title
                            if title = invalid or title = "" then title = programData.title
                            addMarqueeLabel(grid, title, x + 14, y + 2, width - 24, 35, "#FFFFFF", "font:SmallBoldSystemFont")
                            detail = programData.program_details
                            if detail = invalid then detail = ""
                            timeLabel = programData.guide_time
                            if timeLabel = invalid then timeLabel = ""
                            if detail <> "" then timeLabel = timeLabel + " · " + detail
                            addMarqueeLabel(grid, timeLabel, x + 14, y + 37, width - 24, 35, "#AAB9C7", "font:TinySystemFont")
                        end if
                    end if
                end if
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
    timeRange = program.guide_time_range
    if timeRange = invalid then timeRange = ""
    if details <> "" then timeRange = timeRange + " · " + details
    m.top.FindNode("programDetails").text = timeRange
    description = program.program_description
    if description = invalid then description = ""
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
    if m.autoFollowNow and m.guideLoadedSeconds > 0 and m.rows.Count() > 0
        elapsed = now.AsSeconds() - m.guideLoadedSeconds
        shifted = Int((m.windowOffsetSeconds + elapsed) / 10)
        if shifted <> m.lastGuideShift
            m.windowOffsetSeconds = m.windowOffsetSeconds + elapsed
            m.guideLoadedSeconds = now.AsSeconds()
            m.lastGuideShift = shifted
            renderTimeline()
            renderGuideGrid()
        end if
    end if
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
        targetSecond = m.windowOffsetSeconds
        currentPrograms = m.rows[m.currentChannel].programs
        if currentPrograms <> invalid and currentPrograms.Count() > m.currentProgram
            targetSecond = Int((currentPrograms[m.currentProgram].guide_start_second + currentPrograms[m.currentProgram].guide_end_second) / 2)
        end if
        m.currentChannel = m.currentChannel - 1
        if m.currentChannel < 0 then m.currentChannel = 0
        onChannelFocused(invalid)
        m.currentProgram = closestProgramIndex(m.currentChannel, targetSecond)
        showProgram(m.currentProgram)
        renderGuideGrid()
        return true
    else if key = "down"
        targetSecond = m.windowOffsetSeconds
        currentPrograms = m.rows[m.currentChannel].programs
        if currentPrograms <> invalid and currentPrograms.Count() > m.currentProgram
            targetSecond = Int((currentPrograms[m.currentProgram].guide_start_second + currentPrograms[m.currentProgram].guide_end_second) / 2)
        end if
        m.currentChannel = m.currentChannel + 1
        if m.currentChannel >= m.rows.Count() then m.currentChannel = m.rows.Count() - 1
        onChannelFocused(invalid)
        m.currentProgram = closestProgramIndex(m.currentChannel, targetSecond)
        showProgram(m.currentProgram)
        renderGuideGrid()
        return true
    else if key = "right"
        m.autoFollowNow = false
        programs = m.rows[m.currentChannel].programs
        if programs <> invalid and m.currentProgram < programs.Count() - 1 then m.currentProgram = m.currentProgram + 1
        if programs <> invalid and programs.Count() > m.currentProgram then ensureProgramVisible(programs[m.currentProgram])
        showProgram(m.currentProgram)
        renderGuideGrid()
        return true
    else if key = "left"
        m.autoFollowNow = false
        if m.currentProgram > 0 then m.currentProgram = m.currentProgram - 1
        programs = m.rows[m.currentChannel].programs
        if programs <> invalid and programs.Count() > m.currentProgram then ensureProgramVisible(programs[m.currentProgram])
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
