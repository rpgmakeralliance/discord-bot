import datetime
import discord
import asyncio
import logging
import re
import traceback

from collections import namedtuple

from discord.ext import commands
from discord.utils import escape_mentions
from .utilities import config, checks

LinkResult = namedtuple("LinkResult", ["full_url", "host", "extension"])
CHALLENGE_CHANNEL = 529648061937352704
URL_REGEX = r"https?://(?:www.)?([^\s/$?#]+)(?:(?!\.(?:jpg|gif|png|jpeg))[^ \s])*(?:\.((?:jpg|gif|png|jpeg)))?[^\s]*"


def smart_truncate(content: str, length: int = 175, suffix: str = '…'):
    if len(content) <= length:
        return content
    else:
        return ' '.join(content[:length+1].split(' ')[0:-1]) + suffix


async def get_link(content: str):
    search = re.search(URL_REGEX, content, flags=re.I)

    if search is None:
        return None

    groups = search.groups()
    return LinkResult(full_url=search.group(), host=groups[0], extension=groups[1])


class Challenge(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.challenges_database = config.JSONAsset('challenges.json', loop=bot.loop)

        if "current" in self.challenges_database:
            self.actual_challenge = self.challenges_database["current"]
        else:
            self.actual_challenge = None

        self.force_end = False
        self.force_end_votes = False

        self.channel = None
        self.server = None  # TODO: Shouldn't be needed, the GDA guild should be a globally available thing to the bot

        self.challenge_check_task = bot.loop.create_task(self.manage_challenge())


    @commands.Cog.listener()
    async def on_message(self, message):
        await self.add_participation(message)


    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        channel = self.bot.get_channel(int(payload.data["channel_id"]))
        message = await channel.fetch_message(int(payload.message_id))

        await self.add_participation(message)


    async def add_participation(self, message):
        if (not self.actual_challenge or
            not message.channel.id == CHALLENGE_CHANNEL or
            message.author.bot):
            return

        if self.challenges_database[self.actual_challenge]["state"] != "open":
            return

        content = message.content.strip().lower()
        challenge_code = self.actual_challenge.lower()

        if content.startswith(challenge_code) or content.endswith(challenge_code):

            if content == challenge_code and not message.attachments:
                await message.add_reaction('\N{THUMBS DOWN SIGN}')
                return

            self.challenges_database._content[self.actual_challenge][message.author.id] = {"id": str(message.id)}
            await self.challenges_database.save()

            await message.add_reaction('\N{THUMBS UP SIGN}')


    @commands.group(invoke_without_command=True, aliases=["c"])
    async def challenge(self, ctx):
        """
            Affiche le titre du challenge de la semaine en cours, s'il y en a un.
        """
        if self.actual_challenge:
            await ctx.author.send("Le challenge de la semaine en cours est `{}`".format(self.actual_challenge))
        else:
            await ctx.author.send("Aucun challenge de la semaine en cours !")


    @challenge.command(aliases=["s", "new"])
    @checks.has_roles_or_staff()
    async def set(self, ctx, *, challenge: str):
        """
            Applique un nouveau challenge
        """
        now = datetime.datetime.now()
        challenge = "[" + challenge + "]"

        if challenge in self.challenges_database._content:
            await ctx.author.send("Ce challenge a déjà existé!")
            return

        self.actual_challenge = challenge
        self.challenges_database._content[self.actual_challenge] = {
                                            "state": "open",
                                            "dates": {"started": now.strftime("%d/%m/%y"), "ended": ""}
                                        }

        await self.challenges_database.put("current", self.actual_challenge)

        await ctx.author.send("Le challenge de la semaine est maintenant `{}` !".format(self.actual_challenge))


    @challenge.command(aliases=["e"])
    @checks.has_roles_or_staff()
    async def end(self, ctx):
        """
            Termine la période ouverte prématurement
        """
        self.force_end = True
        self.challenge_check_task.cancel()
        self.challenge_check_task = self.bot.loop.create_task(self.manage_challenge())


    @challenge.command(aliases=["ev"])
    @checks.has_roles_or_staff()
    async def end_votes(self, ctx):
        """
            Termine la période de votes prématurement
        """
        self.force_end_votes = True
        self.challenge_check_task.cancel()
        self.challenge_check_task = self.bot.loop.create_task(self.manage_challenge())


    async def manage_challenge(self):
        try:
            while not self.bot.is_closed():
                if self.actual_challenge:
                    now = datetime.datetime.now()

                    if not self.bot.is_ready():
                        await self.bot.wait_until_ready()

                    self.get_channels_server()

                    # If we're Sunday and the challenge is in a open state (or force_end is true), show participations and go into the voting state
                    if (now.weekday() == 0 and self.challenges_database[self.actual_challenge]["state"] == "open") or self.force_end:
                        await self.channel.set_permissions(self.server.default_role, send_messages=False)
                        first_message = await self.channel.send("Les participations au challenge de la semaine sont maintenant fermées ! Place aux votes !")

                        await self.print_participations()

                        embed = discord.Embed(
                            colour=discord.Colour(0xf9ac2f),
                            description="[Revenir en haut]({})".format(first_message.jump_url)
                        )

                        await self.channel.send("Pour voter, mettez un :thumbsup: sur une ou plusieurs participations. Vous avez jusqu'à **lundi soir** ! Bonne chance aux participants !", embed=embed)

                        self.challenges_database[self.actual_challenge]["state"] = "voting"
                        await self.challenges_database.save()
                        self.force_end = False

                    # If we're Monday and the challenge is in a voting state (or force_end_votes is true), show the podium and end the current challenge
                    if (now.weekday() == 1 and self.challenges_database[self.actual_challenge]["state"] == "voting") or self.force_end_votes:
                        await self.print_podium()

                        await self.channel.send("Bien joué à tous les participants ! :clap: À très bientôt pour le prochain challenge de la semaine !")

                        self.challenges_database._content[self.actual_challenge]["state"] = "ended"
                        self.challenges_database._content[self.actual_challenge]["dates"]["ended"] = now.strftime("%d/%m/%y")
                        await self.challenges_database.put("current", "")

                        self.force_end_votes = False
                        self.actual_challenge = None
                        await self.channel.set_permissions(self.server.default_role, send_messages=True)

                await asyncio.sleep(30)

        except asyncio.CancelledError:
            pass
        except (OSError, discord.ConnectionClosed):
            self.challenge_check_task.cancel()
            self.challenge_check_task = self.bot.loop.create_task(self.manage_challenge())
        except Exception:
            print(traceback.format_exc())


    async def print_participations(self):
        async with self.channel.typing():
            for key, value in self.challenges_database[self.actual_challenge].items():
                if key == "state" or key == "dates":
                    continue

                member = message = None

                member = self.server.get_member(int(key))

                # Probably not needed? Normally we wouldn't bother trying that hard but since the goal is for the bot
                # to be more or less 100% automated, but I'd rather avoid something not working due to API issue /shrug
                for i in range(3):
                    try:
                        message = await self.channel.fetch_message(int(value["id"]))
                    except discord.HTTPException:
                        logging.info("discord.HTTPException in getting message {}, try {}".format(value["id"], i))

                        if i < 2:
                            await asyncio.sleep(1)
                            continue
                        else:
                            break
                    break

                if not member:
                    logging.info("Member {} not found!".format(key))
                    continue

                if not message:
                    logging.info("Message {} not found!".format(value["id"]))
                    continue

                # Find the participation content
                image_thumbnail = False
                link = None

                if message.attachments:
                    attachment = message.attachments[0]
                    link = LinkResult(full_url=attachment.url, host="cdn.discordapp.com", extension=None)

                    filename = attachment.filename.lower()

                    if (filename.endswith("png") or
                        filename.endswith("jpg") or
                        filename.endswith("gif") or
                        filename.endswith("jpeg")):
                        image_thumbnail = True

                # If we didn't find anything in the attachments, trying for URL..
                if not link:
                    link = await get_link(message.content)

                    if link:
                        if link.extension:
                            image_thumbnail = link.extension in ["png", "jpg", "gif", "jpeg"]

                # Prepare embed
                description = re.sub(re.escape(self.actual_challenge), "", escape_mentions(message.content), flags=re.I).strip()
                description = smart_truncate(description)

                view_participation = "**[Voir la participation]({})** {}".format(link.full_url if link else message.jump_url, ("("+link.host+")") if link else "")
                view_original_message = "[Voir le message original]({})".format(message.jump_url)

                description = description+"\n\n{}\n{}".format(view_participation, view_original_message)

                e = discord.Embed(
                    colour=discord.Colour(0xf9ac2f),
                    description=description
                )
                e.set_author(name=member.display_name, icon_url=member.avatar_url)

                if image_thumbnail:
                    e.set_thumbnail(url=link.full_url)

                # Send it!!
                end_message = await self.channel.send(embed=e)

                self.challenges_database._content[self.actual_challenge][key]["bot_message_id"] = str(end_message.id)

                await end_message.add_reaction('\N{THUMBS UP SIGN}')
                await asyncio.sleep(1.5)

            await self.challenges_database.save()

    async def print_podium(self):
        end_results = []
        reactions_count = {}

        async with self.channel.typing():
            for key, value in self.challenges_database[self.actual_challenge].items():
                if key == "state" or key == "dates":
                    continue

                # Skip participations that didn't work
                if not "bot_message_id" in value:
                    continue

                message = await self.channel.fetch_message(int(value["bot_message_id"]))
                author = self.server.get_member(int(key))

                reaction = None
                for x in message.reactions:
                    if x.me:
                        reaction = x
                        break

                # This is not the greatest of idea. But. It. Works.
                reactions_count[author.display_name + " (" + author.mention + ")"] = reaction.count

                await asyncio.sleep(1)

            for i in range(3):
                if not reactions_count:
                    break

                max_points = max(reactions_count.values())
                results = [s for s, v in reactions_count.items() if v == max_points]

                end_results.append(results)

                for key in results:
                    del reactions_count[key]

            e = discord.Embed(colour=discord.Colour(0xf9ac2f))
            e.set_thumbnail(url="https://i.imgur.com/lFVTGMe.png")


            if len(end_results) > 0:
                e.add_field(name=":first_place: En première position", value="\n".join(end_results[0]))

            if len(end_results) > 1:
                if end_results[1]:
                    e.add_field(name=":second_place: En deuxième position", value="\n".join(end_results[1]))

            if len(end_results) > 2:
                if end_results[2]:
                    e.add_field(name=":third_place: En troisième position", value="\n".join(end_results[2]))

        await self.channel.send("Les votes sont clos ! Voici les résultats : ", embed=e)

    def get_channels_server(self):
        if not self.channel:
            self.channel = self.bot.get_channel(CHALLENGE_CHANNEL)

        if not self.server:
            # TODO: Guild ID shouldn't be hardcoded in this cog, it should be taken from a globally available source
            self.server = self.bot.get_guild(218745934652112899)


def setup(bot):
    bot.add_cog(Challenge(bot))
