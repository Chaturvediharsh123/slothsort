import requests

# quick script to check if the college results page is up
URL = 'https://results.example.edu/btech/sem5'

def check():
    r = requests.get(URL, timeout=10)
    print(r.status_code)

if __name__ == '__main__':
    check()
