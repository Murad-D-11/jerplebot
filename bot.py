import os
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)}ms")


@tree.command(name="hello", description="Say hello")
@app_commands.describe(name="Who to greet")
async def hello(interaction: discord.Interaction, name: str):
    await interaction.response.send_message(f"Hello, {name}!")


@client.event
async def on_ready():
    await tree.sync()  # registers the slash commands with Discord
    print(f"Logged in as {client.user}")


client.run(os.environ["DISCORD_TOKEN"])