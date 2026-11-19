import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

public final class Main {
    public static void main(String[] args) throws Exception {
        if (args.length == 3 && args[0].equals("prepare")) {
            prepare(Path.of(args[1]), longArg(args[2]));
            return;
        }
        if (args.length == 2 && args[0].equals("reconcile")) {
            System.out.print(Reconciler.reconcile(Path.of(args[1])).render());
            return;
        }
        if (args.length == 3 && args[0].equals("expected")) {
            System.out.print(expected(longArg(args[1]), longArg(args[2])).render());
            return;
        }
        usage();
    }

    private static void prepare(Path output, long eventCount) throws IOException {
        long accounts = accountCount(eventCount);
        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            StringBuilder line = new StringBuilder(96);
            for (long sequence = 1; sequence <= eventCount; sequence++) {
                long timestamp = eventTimestamp(sequence);
                long amount = 1L + sequence % 1_000_003L;
                long account = sequence % accounts;
                line.setLength(0);
                line.append(sequence).append('|')
                        .append(timestamp).append('|')
                        .append(amount).append("|account-")
                        .append(String.format("%07d", account)).append('\n');
                writer.write(line.toString());
            }
        }
    }

    private static Reconciler.Report expected(long eventCount, long accounts) {
        if (eventCount <= 0 || accounts <= 0) {
            throw new IllegalArgumentException("event and account counts must be positive");
        }
        List<Reconciler.AccountSummary> summaries = new ArrayList<>();
        for (long account = 0; account < accounts; account++) {
            long count = eventCount / accounts + (account < eventCount % accounts ? 1 : 0);
            if (count == 0) {
                continue;
            }
            long first = account == 0 ? accounts : account;
            long last = first + (count - 1) * accounts;
            long[] order = new long[(int) count];
            for (int i = 0; i < order.length; i++) {
                long sequence = first + i * accounts;
                long delta = (sequence * 2654435761L) % 10_000_000L;
                order[i] = (delta << 30) + sequence;
            }
            Arrays.sort(order);
            long total = 0;
            long checksum = 0x5deece66dL;
            for (long key : order) {
                long sequence = key & ((1L << 30) - 1);
                long amount = 1L + sequence % 1_000_003L;
                total = Math.addExact(total, amount);
                long value = 31L * checksum + sequence;
                value = 31L * value + eventTimestamp(sequence);
                checksum = 31L * value + amount;
            }
            summaries.add(new Reconciler.AccountSummary(
                    String.format("account-%07d", account), count, total, first, last,
                    eventTimestamp(first), eventTimestamp(last), checksum));
        }
        return new Reconciler.Report(eventCount, summaries);
    }

    private static long eventTimestamp(long sequence) {
        return 1_700_000_000L + (sequence * 2654435761L) % 10_000_000L;
    }

    private static long accountCount(long eventCount) {
        if (eventCount <= 0) {
            throw new IllegalArgumentException("event count must be positive");
        }
        if (eventCount > 1_000_000_000L) {
            throw new IllegalArgumentException("event count is too large");
        }
        return Math.max(1L, eventCount / 1_000L);
    }

    private static long longArg(String text) {
        long value = Long.parseLong(text);
        if (value <= 0) {
            throw new IllegalArgumentException("count must be positive");
        }
        return value;
    }

    private static void usage() {
        System.err.println("usage:");
        System.err.println("  Main prepare <output> <event-count>");
        System.err.println("  Main reconcile <log>");
        System.err.println("  Main expected <event-count> <account-count>");
        System.exit(2);
    }

    private Main() {
    }
}
