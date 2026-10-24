import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import java.io.FileDescriptor;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;

/**
 * Speaker only. Own process: Dotd blocks inside AudioRecord, and this Fire OS
 * kills the process if one app_process starts a second Java thread.
 * A buffer under 32 KB is opened as a DIRECT track on this Dot and underruns
 * into static. 32 KB stays on the mixer (measured underruns: 0).
 */
public class Playd {
    static final int SPK_PORT = 19772;
    static final int RATE = 48000;
    static final int CAP = 32768;
    static final byte[] SILENCE = new byte[1920];

    public static void main(String[] args) throws Exception {
        try {
            String self = new java.io.File("/proc/self").getCanonicalPath();
            java.io.FileOutputStream pid = new java.io.FileOutputStream("/data/local/tmp/playd.pid");
            pid.write(self.substring(self.lastIndexOf('/') + 1).getBytes("UTF-8"));
            pid.close();
        } catch (Exception ignored) {}

        ServerSocketChannel spkS = ServerSocketChannel.open();
        spkS.configureBlocking(false);
        spkS.socket().setReuseAddress(true);
        spkS.socket().bind(new InetSocketAddress("127.0.0.1", SPK_PORT));
        System.out.println("READY");
        System.out.flush();

        ByteBuffer buf = ByteBuffer.allocate(65536);
        byte[] frame = new byte[65537];
        FileDescriptor keys = null;
        try {
            keys = Os.open("/dev/input/event1", OsConstants.O_RDONLY | OsConstants.O_NONBLOCK, 0);
        } catch (Exception e) {
            System.out.println("CUT_FAIL");
            System.out.flush();
        }
        byte[] ev = new byte[16];
        int evGot = 0;
        while (true) {
            SocketChannel spkC = spkS.accept();
            if (spkC == null) {
                Thread.sleep(20);
                continue;
            }
            spkC.configureBlocking(false);
            int min = AudioTrack.getMinBufferSize(
                    RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
            int cap = Math.max(min, CAP) & ~1;
            AudioTrack play = openSpk(cap);
            if (play == null) {
                spkC.close();
                System.exit(1);
            }
            boolean started = false;
            int held = 0;
            int primed = 0;
            // Leave a little free so write() before play() cannot block forever.
            int limit = cap - 2048;
            long deadline = System.currentTimeMillis() + 150;
            while (true) {
                if (keys != null) {
                    try {
                        int k = Os.read(keys, ev, evGot, 16 - evGot);
                        if (k > 0) evGot += k;
                        if (evGot == 16) {
                            if (muteDown(ev)) {
                                // Drop what this track and this socket already have.
                                // Bytes that arrive later, including the close beep, play.
                                held = 0;
                                if (started) {
                                    try { play.pause(); } catch (Exception ignored) {}
                                    try { play.flush(); } catch (Exception ignored) {}
                                    try { play.play(); } catch (Exception ignored) {}
                                }
                                buf.clear();
                                while (true) {
                                    int d;
                                    try { d = spkC.read(buf); } catch (Exception e) { d = -1; }
                                    if (d <= 0) break;
                                    buf.clear();
                                }
                                System.out.println("CUT");
                                System.out.flush();
                            }
                            evGot = 0;
                        }
                    } catch (ErrnoException e) {
                        if (e.errno != OsConstants.EAGAIN) keys = null;
                    }
                }
                int n = 0;
                if (held < 2) {
                    buf.clear();
                    try {
                        n = spkC.read(buf);
                    } catch (Exception e) {
                        n = -1;
                    }
                    if (n < 0) {
                        // The host closed the socket to cut a reply. stop()
                        // alone plays the 32 KB still queued. flush() drops it.
                        held = 0;
                        if (started) {
                            try { play.pause(); } catch (Exception ignored) {}
                            try { play.flush(); } catch (Exception ignored) {}
                        }
                        try { spkC.close(); } catch (Exception ignored) {}
                        try { play.stop(); } catch (Exception ignored) {}
                        play.release();
                        break;
                    }
                    if (n > 0) {
                        System.arraycopy(buf.array(), 0, frame, held, n);
                        held += n;
                    }
                }
                int even = held & ~1;
                if (!started) {
                    int room = limit - primed;
                    if (room < 0) room = 0;
                    int take = even < room ? even : room & ~1;
                    if (take >= 2) {
                        int w = writeAll(play, frame, 0, take);
                        if (w > 0) {
                            primed += w;
                            int left = held - w;
                            if (left > 0) System.arraycopy(frame, w, frame, 0, left);
                            held = left > 0 ? left : 0;
                        }
                    }
                    boolean due = primed > 0 || System.currentTimeMillis() >= deadline;
                    if (due && primed < limit) {
                        int want = limit - primed;
                        if (want > SILENCE.length) want = SILENCE.length;
                        want &= ~1;
                        if (want >= 2) {
                            int w = writeAll(play, SILENCE, 0, want);
                            if (w > 0) primed += w;
                        }
                        continue;
                    }
                    if (!due) {
                        Thread.sleep(5);
                        continue;
                    }
                    play.play();
                    started = true;
                    System.out.println("SPK " + cap);
                    System.out.flush();
                    continue;
                }
                if (even >= 2) {
                    // A long write blocks inside AudioTrack, so the mute key
                    // and a socket close wait until that write returns.
                    // 1920 bytes is about 20 ms. The cut is seen on the next lap.
                    int take = even < SILENCE.length ? even : SILENCE.length;
                    int w = writeAll(play, frame, 0, take);
                    if (w <= 0) {
                        Thread.sleep(5);
                        continue;
                    }
                    int left = held - w;
                    if (left > 0) System.arraycopy(frame, w, frame, 0, left);
                    held = left;
                    continue;
                }
                if (writeAll(play, SILENCE, 0, SILENCE.length) <= 0) Thread.sleep(5);
            }
        }
    }

    static boolean muteDown(byte[] ev) {
        int type = (ev[8] & 0xff) | ((ev[9] & 0xff) << 8);
        int code = (ev[10] & 0xff) | ((ev[11] & 0xff) << 8);
        int value = (ev[12] & 0xff) | ((ev[13] & 0xff) << 8)
                | ((ev[14] & 0xff) << 16) | ((ev[15] & 0xff) << 24);
        return type == 1 && value == 1 && code == 0x71;
    }

    static int writeAll(AudioTrack play, byte[] data, int off, int len) {
        int done = 0;
        while (done < len) {
            int w = play.write(data, off + done, len - done);
            if (w < 0) return w;
            if (w == 0) return done;
            done += w;
        }
        return done;
    }

    static AudioTrack openSpk(int cap) {
        AudioTrack t = new AudioTrack(
                AudioManager.STREAM_MUSIC, RATE,
                AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT,
                cap, AudioTrack.MODE_STREAM);
        if (t.getState() != AudioTrack.STATE_INITIALIZED) {
            System.out.println("SPK_FAIL");
            System.out.flush();
            return null;
        }
        return t;
    }
}
