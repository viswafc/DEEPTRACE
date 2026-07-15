"""
DeepTrace Sample: Optimized Python Code
Demonstrates: O(n log n) sort, minimal allocations, generator usage
"""

def main():
    # Same functional result, optimized implementation
    N = 2000
    data = list(range(N, 0, -1))
    
    # Python's built-in sort (Timsort, O(n log n))
    data.sort()
    
    # String join instead of concat
    result = ','.join(str(x) for x in data[:100])
    
    # Generator instead of list comprehension where possible
    total = sum(x * 2 for x in data)  # no temp list
    
    print(f'Sorted first 5: {data[:5]}, result_len: {len(result)}, total: {total}')

if __name__ == "__main__":
    main()
