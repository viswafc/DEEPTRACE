/**
 * DeepTrace Sample: Inefficient Java
 * Demonstrates: String concatenation in loop, no-StringBuilder, GC pressure
 */
public class Inefficient {
    public static void main(String[] args) {
        int N = 2000;
        int[] data = new int[N];
        for (int i = 0; i < N; i++) data[i] = N - i;
        
        // Bubble sort
        for (int i = 0; i < N; i++) {
            for (int j = 0; j < N - i - 1; j++) {
                if (data[j] > data[j+1]) { 
                    int t = data[j]; 
                    data[j] = data[j+1]; 
                    data[j+1] = t; 
                }
            }
        }
                
        // String concat in loop — creates many String objects
        String result = "";
        for (int x : data) result += x + ",";
        
        // Create and discard many objects — GC pressure
        for (int i = 0; i < 10000; i++) {
            int[] temp = new int[100];
            for (int j=0; j<100; j++) temp[j] = j * i;
        }
        
        System.out.println("Done: " + result.substring(0, Math.min(50, result.length())));
    }
}
