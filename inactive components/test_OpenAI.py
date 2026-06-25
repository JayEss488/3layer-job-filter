from openai import OpenAI

# By leaving the constructor empty, it natively searches os.environ for 'OPENAI_API_KEY'
client = OpenAI()

try:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Connection check: Respond with the word 'Ready' if you can hear me."}],
        max_tokens=10
    )
    print("\n✅ Configuration Successful!")
    print(f"AI Response: {response.choices[0].message.content.strip()}\n")
except Exception as e:
    print("\n❌ Configuration Failed.")
    print(f"Error Details: {e}\n")