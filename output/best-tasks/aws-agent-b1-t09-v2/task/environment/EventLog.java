import java.io.BufferedReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedList;
import java.util.List;

public final class EventLog {
    public record Event(long sequence, String account, long timestamp, long amount) {
        public Event {
            if (sequence < 0) {
                throw new IllegalArgumentException("negative sequence");
            }
            if (account == null || account.isEmpty()) {
                throw new IllegalArgumentException("empty account");
            }
            if (amount < 0) {
                throw new IllegalArgumentException("negative amount");
            }
        }
    }

    public static Event parse(String line, long lineNumber) {
        if (line == null) {
            throw new IllegalArgumentException("missing line " + lineNumber);
        }
        String[] fields = line.split("\\|", -1);
        if (fields.length != 4) {
            throw new IllegalArgumentException("line " + lineNumber + " has " + fields.length + " fields");
        }
        try {
            long sequence = parseFixedLong(fields[0], 19);
            long timestamp = parseFixedLong(fields[1], 18);
            long amount = parseFixedLong(fields[2], 18);
            String account = fields[3];
            if (account.isEmpty() || account.length() > 64) {
                throw new IllegalArgumentException("bad account at line " + lineNumber);
            }
            for (int i = 0; i < account.length(); i++) {
                char c = account.charAt(i);
                if ((c < 'a' || c > 'z') && (c < '0' || c > '9') && c != '-') {
                    throw new IllegalArgumentException("bad account at line " + lineNumber);
                }
            }
            return new Event(sequence, account, timestamp, amount);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("bad number at line " + lineNumber, e);
        }
    }

    private static long parseFixedLong(String text, int maxLength) {
        if (text.isEmpty() || text.length() > maxLength) {
            throw new NumberFormatException("integer length");
        }
        long value = 0;
        for (int i = 0; i < text.length(); i++) {
            char c = text.charAt(i);
            if (c < '0' || c > '9') {
                throw new NumberFormatException("integer digit");
            }
            if (value > (Long.MAX_VALUE - (c - '0')) / 10) {
                throw new NumberFormatException("integer overflow");
            }
            value = value * 10 + (c - '0');
        }
        return value;
    }

    public static List<Event> snapshot(Path input) throws IOException {
        List<Event> events = new LinkedList<>();
        try (BufferedReader reader = Files.newBufferedReader(input, StandardCharsets.UTF_8)) {
            long lineNumber = 0;
            String line;
            while ((line = reader.readLine()) != null) {
                lineNumber++;
                events.add(parse(line, lineNumber));
            }
        }
        return events;
    }

    public static String render(Event event) {
        return event.sequence() + "|" + event.timestamp() + "|" + event.amount() + "|" + event.account();
    }

    @FunctionalInterface
    public interface Visitor {
        void accept(Event event) throws IOException;
    }

    public static void forEach(Path input, Visitor visitor) throws IOException {
        try (BufferedReader reader = Files.newBufferedReader(input, StandardCharsets.UTF_8)) {
            long lineNumber = 0;
            String line;
            while ((line = reader.readLine()) != null) {
                lineNumber++;
                visitor.accept(parse(line, lineNumber));
            }
        }
    }

    private EventLog() {
    }
}
