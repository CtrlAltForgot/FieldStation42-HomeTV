sub init()
    m.top.functionName = "runRequest"
end sub

sub runRequest()
    transfer = CreateObject("roUrlTransfer")
    port = CreateObject("roMessagePort")
    transfer.SetMessagePort(port)
    transfer.SetUrl(m.top.url)
    transfer.SetCertificatesFile("common:/certs/ca-bundle.crt")
    transfer.InitClientCertificates()
    transfer.AddHeader("Accept", "application/json")
    transfer.AddHeader("Content-Type", "application/json")
    method = UCase(m.top.method)
    started = false
    if method = "POST"
        started = transfer.AsyncPostFromString(m.top.body)
    else if method = "DELETE"
        transfer.SetRequest("DELETE")
        started = transfer.AsyncGetToString()
    else
        started = transfer.AsyncGetToString()
    end if
    if not started
        m.top.error = "Could not start the network request"
        return
    end if
    message = Wait(15000, port)
    if type(message) <> "roUrlEvent"
        transfer.AsyncCancel()
        m.top.error = "The myHomeTV server did not respond within 15 seconds"
        return
    end if
    m.top.statusCode = message.GetResponseCode()
    raw = message.GetString()
    if m.top.statusCode >= 200 and m.top.statusCode < 300
        if raw = "" then raw = "{}"
        parsed = ParseJson(raw)
        if parsed = invalid then parsed = {}
        m.top.response = parsed
    else
        detail = "Request failed (" + m.top.statusCode.ToStr() + ")"
        parsed = ParseJson(raw)
        if parsed <> invalid and parsed.detail <> invalid then detail = parsed.detail
        m.top.error = detail
    end if
end sub
