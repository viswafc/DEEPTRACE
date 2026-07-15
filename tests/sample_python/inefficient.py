"""
DeepTrace Sample: Inefficient Python Code
Demonstrates: O(n²) complexity, excessive memory allocation, GC pressure
"""

def main():
    # Bubble sort on large list (O(n²))
    # Creates many temporary list copies
    # String concatenation in loop (creates many string objects)
    # Repeated list comprehensions that could be generators
    # Deliberately triggers GC pressure with many short-lived objects
    # TARGET METRICS: high allocations, multiple GC events, slow runtime
    
    N = 2000
    data = list(range(N, 0, -1))
    
    # Bubble sort
    for i in range(len(data)):
        for j in range(len(data) - i - 1):
            if data[j] > data[j+1]:
                data[j], data[j+1] = data[j+1], data[j]
                
    # String concatenation in loop
    result = ''
    for x in data[:100]:
        result = result + str(x) + ','
        
    # Memory pressure: create and discard many lists
    for _ in range(1000):
        temp = [x * 2 for x in data]
        del temp
        
    print(f'Sorted first 5: {data[:5]}, result_len: {len(result)}')

if __name__ == "__main__":
    main()
