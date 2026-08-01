sub Main(input as Dynamic)
    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.SetMessagePort(port)
    scene = screen.CreateScene("MainScene")
    screen.Show()

    if input <> invalid and input.contentId <> invalid
        scene.launchChannel = input.contentId
    end if

    while true
        message = Wait(0, port)
        if type(message) = "roSGScreenEvent" and message.IsScreenClosed()
            return
        end if
    end while
end sub

