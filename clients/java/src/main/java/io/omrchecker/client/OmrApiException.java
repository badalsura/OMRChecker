package io.omrchecker.client;

/** Non-2xx response from the API (status code plus the server's detail message). */
public class OmrApiException extends RuntimeException {
    private final int status;
    private final String body;

    public OmrApiException(int status, String detail, String body) {
        super("HTTP " + status + ": " + detail);
        this.status = status;
        this.body = body;
    }

    public int getStatus() { return status; }

    public String getBody() { return body; }
}
