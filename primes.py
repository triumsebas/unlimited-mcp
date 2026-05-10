def primes_upto(n):
    sieve = [True] * (n + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(n ** 0.5) + 1):
        if sieve[i]:
            for j in range(i * i, n + 1, i):
                sieve[j] = False
    return [i for i, is_prime in enumerate(sieve) if is_prime]

if __name__ == '__main__':
    print(primes_upto(50))
def get_primes_up_to(n):
    """Return a list of all prime numbers up to and including n."""
    if n < 2:
        return []
    sieve = [True] * (n + 1)
    sieve[0] = sieve[1] = False
    for i in range(2, int(n ** 0.5) + 1):
        if sieve[i]:
            for j in range(i * i, n + 1, i):
                sieve[j] = False
    return [num for num, is_prime in enumerate(sieve) if is_prime]


if __name__ == "__main__":
    primes = get_primes_up_to(50)
    print(primes)
