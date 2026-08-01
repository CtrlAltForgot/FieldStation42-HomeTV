sub init()
    m.top.functionName = "runRequest"
end sub

sub runRequest()
    transfer = CreateObject("roUrlTransfer")
    transfer.SetUrl(m.top.url)
    transfer.SetCertificatesFile("common:/certs/ca-bundle.crt")
    transfer.InitClientCertificates()
    transfer.AddHeader("Accept", "application/json")
    transfer.AddHeader("Content-Type", "application/json")
    method = UCase(m.top.method)
    if method = "POST"
        raw = transfer.PostFromString(m.top.body)
    else if method = "DELETE"
        transfer.SetRequest("DELETE")
        raw = transfer.GetToString()
    else
        raw = transfer.GetToString()
    end if
    m.top.statusCode = transfer.GetResponseCode()
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

