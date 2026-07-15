/**
 * DeepTrace Sample: Optimized Java
 * Demonstrates: Arrays.sort, StringBuilder, minimal object creation
 */
import java.util.Arrays;

public class Optimized {
    public static void main(String[] args) {
        int N = 2000;
        int[] data = new int[N];
        for (int i = 0; i < N; i++) data[i] = N - i;
        
        Arrays.sort(data);
        
        StringBuilder sb = new StringBuilder(N * 5);
        for (int x : data) sb.append(x).append(',');
        
        // Reuse array instead of creating new ones
        int[] reusable = new int[100];
        for (int i = 0; i < 10000; i++) {
            for (int j = 0; j < 100; j++) reusable[j] = j * i;
        }
        
        System.out.println("Done: " + sb.substring(0, Math.min(50, sb.length())));
    }
}
