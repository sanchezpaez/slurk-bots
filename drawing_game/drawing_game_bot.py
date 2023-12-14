

import logging
import os
import random
from collections import defaultdict
from pathlib import Path
from threading import Timer
from time import sleep

import requests
import socketio

import config
from config import TASK_GREETING, TASK_DESCR_A, TASK_DESCR_B, DATA_PATH, N, GAME_MODE, SHUFFLE, SEED

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).parent.resolve()

STARTING_POINTS = 0


class RoomTimers:
    """A number of timed events during the game.

    :param ready_timer: Reminds both players that they have to send
        /ready to begin the game if none of them did so, yet.
        If one player already sent /ready then the other player
        is reminded 30s later that they should do so, too.
    :type ready_timer: Timer
    :param game_timer: Reminds both players that they should come
        to an end and close their discussion by sending /difference.
    :type game_timer: Timer
    :param done_timer: Resets a sent /difference command for one
        player if their partner did not also sent /difference.
    :type done_timer: Timer
    :param last_answer_timer: Used to end the game if one player
        did not answer for a prolonged time.
    :type last_answer_timer: Timer
    """

    def __init__(self):
        self.ready_timer = None
        self.game_timer = None
        self.done_timer = None
        self.last_answer_timer = None


class ImageData(list):
    """Manage the access to image data.

    Mapping from room id to items left for this room.

    Args:
        path (str): Path to a valid tsv file with at least
            two columns per row, containing the image/word
            pairs. Images are represented as urls.
        n (int): Number of images presented per
            participant per room (one at a time).
        game_mode: one of 'same', 'one_blind', 'different',
            specifying whether both players see the same image,
            whether they see different images, or whether one
            player is blind, i.e. does not see any image.
        shuffle (bool): Whether to randomly sample images or
            select them one by one as present in the file.
            If more images are present than required per room
            and participant, the selection is without replacement.
            Otherwise it is with replacement.
        seed (int): Use together with shuffle to
            make the image presentation process reproducible.
    """

    def __init__(self,
                 path=None,
                 n=1,
                 game_mode='same',
                 shuffle=False,
                 seed=None):
        self._path = path
        self._n = n
        self._mode = game_mode
        self._shuffle = shuffle

        self._images = None
        if seed is not None:
            random.seed(seed)

        self._switch_order = self._switch_image_order()
        self.get_word_image_pairs()

    @property
    def n(self):
        return self._n

    @property
    def mode(self):
        return self._mode

    def get_word_image_pairs(self):
        """Create a collection of word/image pair items.

        Each item holds a word and 1 or 2 urls each to one image
        resource. The images will be loaded from there.
        For local testing, you can host the images with python:
        ```python -m SimpleHTTPServer 8000```

        This function remembers previous calls to itself,
        which makes it possible to split a file of items over
        several participants even for not random sampling.

        Returns:
            None
        """
        if self._images is None:
            # first time accessing the file
            # or a new access for each random sample
            self._images = self._image_gen()

        sample = []
        while len(sample) < self._n:
            try:
                pair = next(self._images)
            except StopIteration:
                # we reached the end of the file
                # and start again from the top
                self._images = self._image_gen()
            else:
                sample.append(pair)
        if self._shuffle:
            # implements reservoir sampling
            for img_line, img in enumerate(self._images, self._n):
                rand_line = random.randint(0, img_line)
                if rand_line < self._n:
                    sample[rand_line] = tuple(img)
            self._images = None

        # make sure that for the one_blind mode, the game alternates
        # between who sees the image
        if self._mode == 'one_blind':
            new_sample = []
            for item in sample:
                order = next(self._switch_order)
                if order:
                    # switch the order of images
                    new_sample.append((item[0], item[2], item[1]))
                else:
                    new_sample.append(item)
            self.extend(new_sample)
        else:
            self.extend(sample)

    def _image_gen(self):
        """Generate one image pair at a time."""
        with open(self._path, "r") as infile:
            for line in infile:
                data = line.strip().split("\t")
                if len(data) == 2:
                    if self.mode == 'one_blind':
                        yield data[0], data[1], None
                    elif self.mode == 'same':
                        yield data[0], data[1], data[1]
                    else:
                        raise KeyError("No second image available.")
                elif len(data) > 2:
                    if self.mode == 'one_blind':
                        yield data[0], data[1], None
                    elif self.mode == 'same':
                        yield data[0], data[1], data[1]
                    else:
                        yield data[0], data[1], data[2]

    def _switch_image_order(self):
        """For the mode one_blind, switch who sees an image"""
        last = 0
        while True:
            if last == 0:
                last = 1
            elif last == 1:
                last = 0
            yield last



class Session:
    def __init__(self):
        self.players = list()
        self.player_a = None
        self.player_b = None
        self.player_a_instructions = TASK_DESCR_A.read_text()
        self.player_b_instructions = TASK_DESCR_B.read_text()
        self.images = ImageData(DATA_PATH, N, GAME_MODE, SHUFFLE, SEED)
        self.game_over = False
        self.timer = None
        self.points = {
            "score": STARTING_POINTS,
            "history": [
                {"correct": 0, "wrong": 0, "warnings": 0}
            ]
        }
        self.rounds_left = 4

    def close(self):
        pass

    def pick_player_a(self):
        self.player_a = random.choice(self.players)["id"]

    def pick_player_b(self):
        for player in self.players:
            if player["id"] != self.player_a:
                self.player_b = player["id"]


class SessionManager(defaultdict):
    def create_session(self, room_id):
        self[room_id] = Session()

    def clear_session(self, room_id):
        if room_id in self:
            self[room_id].close()
            self.pop(room_id)


class DrawingBot:
    sio = socketio.Client(logger=True)
    """The ID of the task the bot is involved in."""
    task_id = None
    """The ID of the room where users for this task are waiting."""
    waiting_room = None

    def __init__(self, token, user, host, port):
        """This bot allows two players that are shown two different
        or equal pictures to discuss about what they see and decide
        whether there are differences.

        :param token: A uuid; a string following the same pattern
            as `0c45b30f-d049-43d1-b80d-e3c3a3ca22a0`
        :type token: str
        :param user: ID of a `User` object that was created with
        the token.
        :type user: int
        :param uri: Full URL including protocol and hostname,
            followed by the assigned port if any.
        :type uri: str
        :param players_per_room: Each room is mapped to a list of
            users. Each user is represented as a dict with the
            keys 'name', 'id', 'msg_n' and 'status'.
        :type players_per_room: dict
        """
        self.token = token
        self.user = user
        self.sessions = SessionManager(Session)
        self.uri = host
        if port is not None:
            self.uri += f":{port}"
        self.uri += "/slurk/api"

        self.players_per_room = dict()
        self.received_waiting_token = set()

        LOG.info(f"Running drawing game bot on {self.uri} with token {self.token}")
        # register all event handlers
        self.register_callbacks()

    def run(self):
        # establish a connection to the server
        self.sio.connect(
            self.uri,
            headers={"Authorization": f"Bearer {self.token}", "user": self.user},
            namespaces="/",
        )
        # wait until the connection with the server ends
        self.sio.wait()

    def register_callbacks(self):
        @self.sio.event
        def new_task_room(data):
            """Triggered after a new task room is created.

            An example scenario would be that the concierge
            bot emitted a room_created event once enough
            users for a task have entered the waiting room.
            """
            room_id = data["room"]
            task_id = data["task"]

            LOG.debug(f"A new task room was created with id: {data['task']}")
            LOG.debug(f"This bot is looking for task id: {self.task_id}")

            if task_id is not None and task_id == self.task_id:
                for usr in data["users"]:
                    self.received_waiting_token.discard(usr["id"])

                # create image items for this room
                LOG.debug("Create data for the new task room...")

                # Resize screen
                self.move_divider(room_id, 30, 70)

                # create a new session for these users
                self.sessions.create_session(room_id)

                # self.images_per_room.get_image_pairs(room_id)
                self.players_per_room[room_id] = []
                for usr in data["users"]:
                    self.sessions[room_id].players.append(
                        {**usr, "msg_n": 0, "status": "joined"}
                    )
                    self.players_per_room[room_id].append(
                        {**usr, "msg_n": 0, "status": "joined"}
                    )
                # self.last_message_from[room_id] = None

                # self.sio.emit(
                #         "text",
                #         {
                #             "message": "Are you ready? "
                #             "Please type **/ready** to begin the game.",
                #             "room": room_id,
                #             "html": True,
                #         },
                # )
                response = requests.post(
                    f"{self.uri}/users/{self.user}/rooms/{room_id}",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                if not response.ok:
                    LOG.error(
                        f"Could not let drawing game bot join room: {response.status_code}"
                    )
                    response.raise_for_status()
                LOG.debug("Sending drawing game bot to new room was successful.")
                self.sio.emit(
                    "message_command",
                    {"command": {"command": "drawing_game_init"}, "room": room_id},
                )
                # self.start_game(room_id)


        @self.sio.event
        def joined_room(data):
            """Triggered once after the bot joins a room."""
            room_id = data["room"]

            # read out task greeting
            message = TASK_GREETING.read_text()
            self.sio.emit(
                "text",
                {
                    "message": message, "room": room_id, "html": True,
                }
            )

        @self.sio.event
        def status(data):
            """Triggered if a user enters or leaves a room."""
            # check whether the user is eligible to join this task
            task = requests.get(
                f"{self.uri}/users/{data['user']['id']}/task",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if not task.ok:
                LOG.error(f"Could not set task instruction title: {task.status_code}")
                task.raise_for_status()
            if not task.json() or task.json()["id"] != int(self.task_id):
                return

            room_id = data["room"]
            # someone joined waiting room
            if room_id == self.waiting_room:
                pass
                # if self.waiting_timer is not None:
                #     LOG.debug("Waiting Timer stopped.")
                #     self.waiting_timer.cancel()
                # if data["type"] == "join":
                #     LOG.debug("Waiting Timer restarted.")

            # some joined a task room
            else:
                curr_usr, other_usr = self.players_per_room[room_id]
                if curr_usr["id"] != data["user"]["id"]:
                    curr_usr, other_usr = other_usr, curr_usr

                if data["type"] == "join":
                    # inform game partner about the rejoin event
                    self.sio.emit(
                        "text",
                        {
                            "message": f"{curr_usr['name']} has joined the game. ",
                            "room": room_id,
                            "receiver_id": other_usr["id"],
                        },
                    )
                elif data["type"] == "leave":
                    # send a message to the user that was left alone
                    self.sio.emit(
                        "text",
                        {
                            "message": f"{curr_usr['name']} has left the game. "
                            "Please wait a bit, your partner may rejoin.",
                            "room": room_id,
                            "receiver_id": other_usr["id"],
                        },
                    )

        @self.sio.event
        def text_message(data):
            """Triggered once a text message is sent (no leading /).

            Count user text messages.
            If encountering something that looks like a command
            then pass it on to be parsed as such.
            """
            LOG.debug(f"Received a message from {data['user']['name']}.")

            room_id = data["room"]
            user_id = data["user"]["id"]

            # filter irrelevant messages
            if user_id == self.user:
                return

            # if the message is part of the main discussion count it
            for usr in self.players_per_room[room_id]:
                if usr["id"] == user_id and usr["status"] == "ready":
                    usr["msg_n"] += 1

        @self.sio.event
        def command(data):
            """Parse user commands."""
            LOG.debug(
                f"Received a command from {data['user']['name']}: {data['command']}"
            )

            room_id = data["room"]
            user_id = data["user"]["id"]
            this_session = self.sessions[room_id]

            if data["command"].lower() == "done":
                pass
                #todo: self._command_done(room_id, user_id)

            elif this_session.rounds_left == 4:
                if "ready" in data["command"].lower():
                    # self.sio.emit(
                    #     "message_command",
                    #     {"command": {"command": "drawing_game_init"}, "room": room_id},
                    # )
                    self._command_ready(room_id, user_id)
                    return
            if isinstance(data["command"], dict):
                pass
            else:
                self.sio.emit(
                    "text",
                    {
                        "message": "Sorry, but I do not understand this command.",
                        "room": room_id,
                        "receiver_id": user_id,
                    },
                )

    def _command_ready(self, room_id, user_id):
        """Must be sent to begin a conversation."""
        # identify the user that has not sent this event
        curr_usr, other_usr = self.players_per_room[room_id]
        if curr_usr["id"] != user_id:
            curr_usr, other_usr = other_usr, curr_usr

        # only one user has sent /ready repetitively
        if curr_usr["status"] in {"ready", "done"}:
            sleep(0.5)
            self.sio.emit(
                "text",
                {
                    "message": "You have already typed 'ready'.",
                    "receiver_id": curr_usr["id"],
                    "room": room_id,
                },
            )
            return
        curr_usr["status"] = "ready"

        # self.timers_per_room[room_id].ready_timer.cancel()
        # a first ready command was sent
        if other_usr["status"] == "joined":
            sleep(0.5)
            # give the user feedback that his command arrived
            self.sio.emit(
                "text",
                {
                    "message": "Now, waiting for your partner to type 'ready'.",
                    "receiver_id": curr_usr["id"],
                    "room": room_id,
                },
            )
            self.sio.emit(
                    "text",
                    {
                        "message": "Your partner is ready. Please, type 'ready'!",
                        "room": room_id,
                        "receiver_id": other_usr["id"],
                    },
            )
        else:
            # both users are ready and the game begins
            self.sio.emit(
                "text",
                {"message": "Woo-Hoo! The game will begin now.", "room": room_id},
            )
            sleep(1)
            self.sessions[room_id].rounds_left = 3
            self.start_game(room_id)
            # self.show_item(room_id)
            # self.sio.emit(
            #     "message_command",
            #     {"command": {"command": "drawing_game_init"}, "room": room_id},
            # )
    def send_individualised_instructions(self, room_id):
        this_session = self.sessions[room_id]

        # Send explainer_ instructions to player_a
        response = requests.patch(f"{self.uri}/rooms/{room_id}/text/instr_title",
                                  json={"text": "Describe the grid",
                                        "receiver_id": this_session.player_a},
                                  headers={"Authorization": f"Bearer {self.token}"},
                                  )
        if not response.ok:
            LOG.error(
                f"Could not set task instruction title: {response.status_code}"
            )
            response.raise_for_status()

        response_e = requests.patch(
            f"{self.uri}/rooms/{room_id}/text/instr",
            json={
                "class": "collapsible-content",
                "text": TASK_DESCR_A.read_text(),
                "receiver_id": this_session.player_a,
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        # HTTPError: 422 Client Error: UNPROCESSABLE ENTITY for url: http://127.0.0.1:5000/slurk/api/rooms/34/text/instr

        if not response_e.ok:
            LOG.error(f"Could not set task instruction: {response_e.status_code}")
            response_e.raise_for_status()

        # Send drawer_ instructions to player_b
        response = requests.patch(f"{self.uri}/rooms/{room_id}/text/instr_title",
                                  json={"text": "Draw the described grid",
                                        "receiver_id": this_session.player_b},
                                  headers={"Authorization": f"Bearer {self.token}"},
                                  )
        if not response.ok:
            LOG.error(
                f"Could not set task instruction title: {response.status_code}"
            )
            response.raise_for_status()

        response_g = requests.patch(
            f"{self.uri}/rooms/{room_id}/text/instr",
            json={
                "class": "collapsible-content",
                "text": TASK_DESCR_B.read_text(),
                "receiver_id": this_session.player_b,
            },
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response_g.ok:
            LOG.error(f"Could not set task instruction: {response_g.status_code}")
            response_g.raise_for_status()

    def start_game(self, room_id):
        this_session = self.sessions[room_id]
        # 1) Choose players A and B
        self.sessions[room_id].pick_player_a()
        self.sessions[room_id].pick_player_b()
        for user in this_session.players:
            if user["id"] == this_session.player_a:
                LOG.debug(f'{user["name"]} is player A.')
            else:
                LOG.debug(f'{user["name"]} is player B.')

        self.send_individualised_instructions(room_id)

        self.show_item(room_id)

    @staticmethod
    def request_feedback(response, action):
        if not response.ok:
            LOG.error(f"Could not {action}: {response.status_code}")
            response.raise_for_status()
        else:
            LOG.debug(f"Successfully did {action}.")

    def move_divider(self, room_id, chat_area=50, task_area=50):
        """move the central divider and resize chat and task area
        the sum of char_area and task_area must sum up to 100
        """
        if chat_area + task_area != 100:
            LOG.error("Could not resize chat and task area: invalid parameters.")
            raise ValueError("chat_area and task_area must sum up to 100")

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/sidebar",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"attribute": "style", "value": f"width: {task_area}%"},
        )
        self.request_feedback(response, "resize sidebar")

        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/content",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"attribute": "style", "value": f"width: {chat_area}%"},
        )
        self.request_feedback(response, "resize content area")

    def _not_done(self, room_id, user_id):
        """One of the two players was not done."""
        for usr in self.players_per_room[room_id]:
            if usr["id"] == user_id:
                usr["status"] = "ready"
        self.sio.emit(
            "text",
            {
                "message": "Your partner seems to still want to discuss some more. "
                "Send 'done' again once you two are really finished.",
                "receiver_id": user_id,
                "room": room_id,
            },
        )

    def show_item(self, room_id):
        """Update the image of the players."""
        LOG.debug("Update the image and task description of the players.")
        # guarantee fixed user order - necessary for update due to rejoin
        # users = sorted(self.sessions[room_id].players, key=lambda x: x["id"])
        # user_1 = users[0]
        # user_2 = users[1]

        this_session = self.sessions[room_id]

        if this_session.images:
            word, image_1 = this_session.images[0]
            LOG.debug(f"{image_1}")

            # show a different image to each user. one image can be None

            # remove image and description for both
            self._hide_image(room_id)
            self._hide_image_desc(room_id)

            # Player 1
            if image_1:
                response = requests.patch(
                    f"{self.uri}/rooms/{room_id}/attribute/id/current-image",
                    json={
                        "attribute": "src",
                        "value": image_1,
                        "receiver_id": this_session.player_a,
                    },
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "set image 1")
                # enable the image
                response = requests.delete(
                    f"{self.uri}/rooms/{room_id}/class/image-area",
                    json={"class": "dis-area", "receiver_id": this_session.player_a},
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "enable image 1")

            else:
                # enable the explanatory text
                response = requests.delete(
                    f"{self.uri}/rooms/{room_id}/class/image-desc",
                    json={"class": "dis-area", "receiver_id": this_session.player_a},
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.request_feedback(response, "enable explanation")

            # Player 2
            # if image_2:
            #     response = requests.patch(
            #         f"{self.uri}/rooms/{room_id}/attribute/id/current-image",
            #         json={
            #             "attribute": "src",
            #             "value": image_2,
            #             "receiver_id": this_session.player_b,
            #         },
            #         headers={"Authorization": f"Bearer {self.token}"},
            #     )
            #     self.request_feedback(response, "set image 2")
            #     # enable the image
            #     response = requests.delete(
            #         f"{self.uri}/rooms/{room_id}/class/image-area",
            #         json={"class": "dis-area", "receiver_id": this_session.player_b},
            #         headers={"Authorization": f"Bearer {self.token}"},
            #     )
            #     self.request_feedback(response, "enable image 2")
            # else:
            # enable the explanatory text
            response = requests.delete(
                f"{self.uri}/rooms/{room_id}/class/image-desc",
                json={"class": "dis-area", "receiver_id": this_session.player_b},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.request_feedback(response, "enable explanation")

    def _hide_image(self, room_id):
        response = requests.post(
            f"{self.uri}/rooms/{room_id}/class/image-area",
            json={"class": "dis-area"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "hide image")

    def _hide_image_desc(self, room_id):
        response = requests.post(
            f"{self.uri}/rooms/{room_id}/class/image-desc",
            json={"class": "dis-area"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.request_feedback(response, "hide description")

    def close_game(self, room_id):
        """Erase any data structures no longer necessary."""
        self.sio.emit(
            "text",
            {
                "message": "You will be moved out of this room ",
                "room": room_id,
            },
        )
        self.room_to_read_only(room_id)

        for usr in self.players_per_room[room_id]:
            self.rename_users(usr["id"])

            response = requests.post(
                f"{self.uri}/users/{usr['id']}/rooms/{self.waiting_room}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if not response.ok:
                LOG.error(
                    f"Could not let user join waiting room: {response.status_code}"
                )
                response.raise_for_status()
            LOG.debug("Sending user to waiting room was successful.")

            response = requests.delete(
                f"{self.uri}/users/{usr['id']}/rooms/{room_id}",
                headers={
                    "If-Match": response.headers["ETag"],
                    "Authorization": f"Bearer {self.token}",
                },
            )
            if not response.ok:
                LOG.error(
                    f"Could not remove user from task room: {response.status_code}"
                )
                response.raise_for_status()
            LOG.debug("Removing user from task room was successful.")

        # remove any task room specific objects
        self.players_per_room.pop(room_id)

    def room_to_read_only(self, room_id):
        """Set room to read only."""
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "readonly", "value": "True"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response.ok:
            LOG.error(f"Could not set room to read_only: {response.status_code}")
            response.raise_for_status()
        response = requests.patch(
            f"{self.uri}/rooms/{room_id}/attribute/id/text",
            json={"attribute": "placeholder", "value": "This room is read-only"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if not response.ok:
            LOG.error(f"Could not set room to read_only: {response.status_code}")
            response.raise_for_status()

    def rename_users(self, user_id):
        """Give all users in a room a new random name."""
        names_f = os.path.join(ROOT, "data", "names.txt")
        with open(names_f, "r", encoding="utf-8") as f:
            names = [line.rstrip() for line in f]

            new_name = random.choice(names)

            response = requests.get(
                f"{self.uri}/users/{user_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if not response.ok:
                LOG.error(f"Could not get user: {response.status_code}")
                response.raise_for_status()

            response = requests.patch(
                f"{self.uri}/users/{user_id}",
                json={"name": new_name},
                headers={
                    "If-Match": response.headers["ETag"],
                    "Authorization": f"Bearer {self.token}",
                },
            )
            if not response.ok:
                LOG.error(f"Could not rename user: {response.status_code}")
                response.raise_for_status()
            LOG.debug(f"Successfuly renamed user to '{new_name}'.")
