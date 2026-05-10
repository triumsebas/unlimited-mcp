def prime_numbers(limit=50):
    primes = []
    for num in range(2, limit+1):
        is_prime = True
        for i in range(2, int(num**0.5)+1):
            if num % i == 0:
                is_prime = False
                break
        if is_prime:
            primes.append(num)
    return primes

if __name__ == '__main__':
    primes = prime_numbers()
    print(primes)
