import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import java.io.FileDescriptor;
import java.net.InetSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;

/**
 * Mic and the mute key. Speaker is Playd: this thread blocks in AudioRecord,
 * and app_process on this Fire OS dies if it starts a second Java thread.
 */
public class Dotd {
    static final int MIC_PORT = 19771;

    public static void main(String[] args) throws Exception {
        try {
            String self = new java.io.File("/proc/self").getCanonicalPath();
            java.io.FileOutputStream pid = new java.io.FileOutputStream("/data/local/tmp/dotd.pid");
            pid.write(self.substring(self.lastIndexOf('/') + 1).getBytes("UTF-8"));
            pid.close();
        } catch (Exception ignored) {}

        ServerSocketChannel micS = ServerSocketChannel.open();
        micS.configureBlocking(false);
        micS.socket().setReuseAddress(true);
        micS.socket().bind(new InetSocketAddress("127.0.0.1", MIC_PORT));

        FileDescriptor keys = null;
        try {
            keys = Os.open("/dev/input/event1", OsConstants.O_RDONLY | OsConstants.O_NONBLOCK, 0);
            System.out.println("INPUT");
            System.out.flush();
        } catch (Exception e) {
            System.out.println("INPUT_FAIL " + e.getMessage());
            System.out.flush();
        }

        System.out.println("READY");
        System.out.flush();

        SocketChannel micC = null;
        AudioRecord rec = null;
        byte[] ev = new byte[16];
        int evGot = 0;
        ByteBuffer micBuf = ByteBuffer.allocate(640);
        // The mute key is also Amazon privacy. That path calls
        // AudioALSAHardware.setMicMute(1) and AudioRecord then returns zeros.
        // Tinymix ADC mutes stay Off, so only this call clears it.
        java.lang.reflect.Method unmute = null;
        long nextUnmute = 0;

        while (true) {
            if (micC == null) {
                micC = micS.accept();
                if (micC != null) micC.configureBlocking(false);
            }

            if (keys != null) {
                try {
                    int n = Os.read(keys, ev, evGot, 16 - evGot);
                    if (n > 0) evGot += n;
                    if (evGot == 16) {
                        onKey(ev);
                        evGot = 0;
                    }
                } catch (ErrnoException e) {
                    if (e.errno != OsConstants.EAGAIN) {
                        System.out.println("INPUT_FAIL " + e.getMessage());
                        System.out.flush();
                        keys = null;
                    }
                }
            }

            if (micC != null) {
                if (rec == null) rec = openMic();
                if (rec != null) {
                    long now = android.os.SystemClock.uptimeMillis();
                    if (now >= nextUnmute) {
                        nextUnmute = now + 100;
                        try {
                            if (unmute == null) {
                                unmute = Class.forName("android.media.AudioSystem")
                                        .getMethod("muteMicrophone", boolean.class);
                            }
                            unmute.invoke(null, Boolean.FALSE);
                        } catch (Throwable t) {
                            System.out.println("MIC_UNMUTE_FAIL " + t.getClass().getSimpleName());
                            System.out.flush();
                            nextUnmute = now + 5000;
                        }
                    }
                    int n = rec.read(micBuf.array(), 0, micBuf.capacity());
                    if (n > 0) {
                        micBuf.clear();
                        micBuf.limit(n);
                        try {
                            // Drop what the host is not reading. A blocking write
                            // stalls the mute key, because this process is one thread.
                            micC.write(micBuf);
                        } catch (Exception e) {
                            micC.close();
                            micC = null;
                            rec.stop();
                            rec.release();
                            rec = null;
                        }
                    } else {
                        Thread.sleep(10);
                    }
                } else {
                    Thread.sleep(30);
                }
            } else {
                Thread.sleep(30);
            }
        }
    }

    static void onKey(byte[] ev) {
        int type = (ev[8] & 0xff) | ((ev[9] & 0xff) << 8);
        int code = (ev[10] & 0xff) | ((ev[11] & 0xff) << 8);
        int value = (ev[12] & 0xff) | ((ev[13] & 0xff) << 8)
                | ((ev[14] & 0xff) << 16) | ((ev[15] & 0xff) << 24);
        if (type == 1 && value == 1 && code == 0x71) {
            System.out.println("MUTE down");
            System.out.flush();
        }
    }

    static AudioRecord openMic() {
        int rate = 16000;
        int ch = AudioFormat.CHANNEL_IN_MONO;
        int enc = AudioFormat.ENCODING_PCM_16BIT;
        int[] srcs = new int[] {
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION
        };
        String[] names = new String[] {"MIC", "VOICE_COMMUNICATION"};
        for (int i = 0; i < srcs.length; i++) {
            int min = AudioRecord.getMinBufferSize(rate, ch, enc);
            if (min < 1) continue;
            AudioRecord rec = new AudioRecord(srcs[i], rate, ch, enc, Math.max(min, rate));
            if (rec.getState() == AudioRecord.STATE_INITIALIZED) {
                rec.startRecording();
                System.out.println("MIC " + names[i]);
                System.out.flush();
                return rec;
            }
            rec.release();
        }
        System.out.println("MIC_FAIL");
        System.out.flush();
        return null;
    }
}
