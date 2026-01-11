"""Steam and ProtonDB API helper functions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import cast

import aiohttp

MAX_CONCURRENT_REQUESTS = 20
"""Limit concurrent requests to avoid rate limiting."""

from vapor.cache_handler import Cache
from vapor.data_structures import (
	HTTP_BAD_REQUEST,
	HTTP_FORBIDDEN,
	HTTP_SUCCESS,
	HTTP_UNAUTHORIZED,
	RATING_DICT,
	STEAM_USER_ID_LENGTH,
	AntiCheatAPIResponse,
	AntiCheatData,
	AntiCheatStatus,
	Game,
	ProtonDBAPIResponse,
	Response,
	SteamAPINameResolutionResponse,
	SteamAPIPlatformsResponse,
	SteamAPIUserDataResponse,
	SteamUserData,
)
from vapor.exceptions import InvalidIDError, PrivateAccountError, UnauthorizedError


async def async_get(
	url: str,
	session: aiohttp.ClientSession | None = None,
) -> Response:
	"""Async get request for fetching web content.

	Args:
		url (str): The URL to fetch data from.
		session (aiohttp.ClientSession | None): Optional session to reuse.
			If None, creates a new session. Defaults to None.

	Returns:
		Response: A Response object containing the body and status code.
	"""
	if session is None:
		async with aiohttp.ClientSession() as session, session.get(url) as response:
			return Response(data=await response.text(), status=response.status)
	else:
		async with session.get(url) as response:
			return Response(data=await response.text(), status=response.status)


async def check_game_is_native(
	app_id: str,
	session: aiohttp.ClientSession | None = None,
) -> bool:
	"""Check if a given Steam game has native Linux support.

	Args:
		app_id (int): The App ID of the game.
		session (aiohttp.ClientSession | None): Optional session to reuse.

	Returns:
		bool: Whether or not the game has native Linux support.
	"""
	data = await async_get(
		f'https://store.steampowered.com/api/appdetails?appids={app_id}&filters=platforms',
		session,
	)
	if data.status != HTTP_SUCCESS:
		return False

	json_data = cast(dict[str, SteamAPIPlatformsResponse], json.loads(data.data))

	# extract whether or not a game is Linux native
	if str(app_id) not in json_data:
		return False

	game_data = json_data[str(app_id)]
	return game_data.get('success', False) and game_data['data']['platforms'].get(
		'linux',
		False,
	)


async def get_anti_cheat_data() -> Cache | None:
	"""Get the anti-cheat data from cache.

	If expired, this function will fetch new data and write that to cache.

	Returns:
		Cache | None: The cache containing anti-cheat data.
	"""
	cache = Cache().load_cache()
	if cache.has_anticheat_cache:
		return cache

	data = await async_get(
		'https://raw.githubusercontent.com/AreWeAntiCheatYet/AreWeAntiCheatYet/master/games.json',
	)

	if data.status != HTTP_SUCCESS:
		return None

	try:
		anti_cheat_data = cast(list[AntiCheatAPIResponse], json.loads(data.data))
	except json.JSONDecodeError:
		return None

	# parse the data from AreWeAntiCheatYet
	deserialized_data = [
		AntiCheatData(
			app_id=game['storeIds']['steam'],
			status=AntiCheatStatus(game['status']),
		)
		for game in anti_cheat_data
		if 'steam' in game['storeIds']
	]

	cache.update_cache(anti_cheat_list=deserialized_data)

	return cache


async def get_game_average_rating(
	app_id: str,
	cache: Cache,
	session: aiohttp.ClientSession | None = None,
) -> str:
	"""Get the average game rating from ProtonDB.

	Args:
		app_id (str): The game ID.
		cache (Cache): The game cache.
		session (aiohttp.ClientSession | None): Optional session to reuse.

	Returns:
		str: A text rating from ProtonDB. gold, bronze, silver, etc.
	"""
	if cache.has_game_cache:
		game = cache.get_game_data(app_id)
		if game is not None:
			return game.rating

	if await check_game_is_native(app_id, session):
		return 'native'

	data = await async_get(
		f'https://www.protondb.com/api/v1/reports/summaries/{app_id}.json',
		session,
	)
	if data.status != HTTP_SUCCESS:
		return 'pending'

	json_data = cast(ProtonDBAPIResponse, json.loads(data.data))

	return json_data.get('tier', 'pending')


async def resolve_vanity_name(api_key: str, name: str) -> str:
	"""Resolve a Steam vanity name into a Steam user ID.

	Args:
		api_key (str): The Steam API key.
		name (str): The user's vanity name.

	Raises:
		UnauthorizedError: If an invalid Steam API key is provided.
		InvalidIDError: If an invalid Steam vanity URL is provided.

	Returns:
		str: The Steam ID of the user.
	"""
	data = await async_get(
		f'https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/?key={api_key}&vanityurl={name}',
	)

	if data.status == HTTP_FORBIDDEN:
		raise UnauthorizedError

	user_data = cast(SteamAPINameResolutionResponse, json.loads(data.data))
	if 'response' not in user_data or user_data['response']['success'] != 1:
		raise InvalidIDError

	return user_data['response']['steamid']


async def get_steam_user_data(
	api_key: str,
	user_id: str,
	on_games_loaded: Callable[[list[Game]], Awaitable[None]] | None = None,
	on_game_updated: Callable[[Game], Awaitable[None]] | None = None,
) -> SteamUserData:
	"""Fetch a steam user's games and get their ratings from ProtonDB.

	Args:
		api_key (str): Steam API key.
		user_id (str): The user's Steam ID or vanity name.
		on_games_loaded (Callable[[list[Game]], Awaitable[None]] | None): Optional
			callback that receives all games initially (with pending ratings).
		on_game_updated (Callable[[Game], Awaitable[None]] | None): Optional
			callback that receives individual games as their ratings load.

	Raises:
		InvalidIDError: If an invalid Steam ID is provided.
		UnauthorizedError: If an invalid Steam API key is provided.

	Returns:
		SteamUserData: The Steam user's data.
	"""
	# check if ID is a Steam ID or vanity URL
	if len(user_id) != STEAM_USER_ID_LENGTH or not user_id.startswith('76561198'):
		try:
			user_id = await resolve_vanity_name(api_key, user_id)
		except UnauthorizedError as e:
			raise UnauthorizedError from e
		except InvalidIDError:
			pass

	cache = Cache().load_cache()

	data = await async_get(
		f'http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={api_key}&steamid={user_id}&format=json&include_appinfo=1&include_played_free_games=1',
	)
	if data.status == HTTP_BAD_REQUEST:
		raise InvalidIDError
	if data.status == HTTP_UNAUTHORIZED:
		raise UnauthorizedError

	user_data = cast(SteamAPIUserDataResponse, json.loads(data.data))

	return await _parse_steam_user_games(
		user_data, cache, on_games_loaded, on_game_updated
	)


async def _fetch_single_game_rating(
	game: dict,
	cache: Cache,
	semaphore: asyncio.Semaphore,
	session: aiohttp.ClientSession,
) -> Game:
	"""Fetch rating for a single game with concurrency control.

	Args:
		game (dict): Game data from Steam API.
		cache (Cache): The game cache.
		semaphore (asyncio.Semaphore): Semaphore to limit concurrent requests.
		session (aiohttp.ClientSession): Shared session to reuse.

	Returns:
		Game: The game with its ProtonDB rating.
	"""
	async with semaphore:
		try:
			rating = await get_game_average_rating(str(game['appid']), cache, session)
		except Exception:
			rating = 'pending'
		return Game(
			name=game['name'],
			rating=rating,
			playtime=game['playtime_forever'],
			app_id=str(game['appid']),
		)


async def _parse_steam_user_games(
	data: SteamAPIUserDataResponse,
	cache: Cache,
	on_games_loaded: Callable[[list[Game]], Awaitable[None]] | None = None,
	on_game_updated: Callable[[Game], Awaitable[None]] | None = None,
) -> SteamUserData:
	"""Parse user data from the Steam API and return information on their games.

	Args:
		data (SteamAPIUserDataResponse): user data from the Steam API
		cache (Cache): the loaded Cache file
		on_games_loaded (Callable[[list[Game]], Awaitable[None]] | None): Optional
			callback that receives all games initially with pending ratings.
		on_game_updated (Callable[[Game], Awaitable[None]] | None): Optional
			callback that receives individual games as their ratings load.

	Returns:
		SteamUserData: the user's Steam games and ProtonDB ratings

	Raises:
		PrivateAccountError: if `games` is not present in `data['response']`
			(the user's account was found but is private)
	"""
	game_data = data['response']

	if 'games' not in game_data:
		raise PrivateAccountError

	games = game_data['games']

	# First, create all games with 'loading' rating and notify UI
	game_ratings: list[Game] = [
		Game(
			name=game['name'],
			rating='loading',
			playtime=game['playtime_forever'],
			app_id=str(game['appid']),
		)
		for game in games
	]

	# Sort by playtime descending before showing
	game_ratings.sort(key=lambda x: x.playtime, reverse=True)

	# Show all games immediately with 'pending' rating
	if on_games_loaded:
		await on_games_loaded(game_ratings)

	# Now fetch actual ratings concurrently, streaming updates as they complete
	semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
	ratings_map: dict[str, str] = {}

	async with aiohttp.ClientSession() as session:
		tasks = [
			_fetch_single_game_rating(game, cache, semaphore, session)
			for game in games
		]

		# Stream results as they complete
		for coro in asyncio.as_completed(tasks):
			fetched_game = await coro
			ratings_map[fetched_game.app_id] = fetched_game.rating
			if on_game_updated:
				await on_game_updated(fetched_game)

	# Build final sorted list with all ratings
	game_ratings = [
		Game(
			name=g.name,
			rating=ratings_map.get(g.app_id, 'pending'),
			playtime=g.playtime,
			app_id=g.app_id,
		)
		for g in game_ratings
	]

	game_ratings.sort(key=lambda x: x.playtime)
	game_ratings.reverse()

	# remove all of the games that we used that were already cached
	# this ensures that the timestamps of those games don't get updated
	game_ratings_copy = game_ratings.copy()
	games_to_remove: list[Game] = [
		game
		for game in game_ratings_copy
		if cache.get_game_data(game.app_id) is not None
	]

	# we do this in a seperate loop so that we're not mutating the
	# iterable during iteration
	for game in games_to_remove:
		game_ratings_copy.remove(game)

	# update the game cache
	cache.update_cache(game_list=game_ratings)

	# compute user average
	game_rating_nums = [RATING_DICT[game.rating][0] for game in game_ratings]
	user_average = round(sum(game_rating_nums) / len(game_rating_nums))
	user_average_text = next(
		key for key, value in RATING_DICT.items() if value[0] == user_average
	)

	return SteamUserData(game_ratings=game_ratings, user_average=user_average_text)
