from dataclasses import dataclass, field
from typing import Literal

Submenu = Literal[
    "root",
    "friends",
    "friends_invite",
    "friends_remove",
    "help",
    "help_feedback",
    "wishlist",
    "wishlist_add",
    "wishlist_delete",
]

TOP_LEVEL_MENUS: set[Submenu] = {"friends", "wishlist", "help"}
SUBMENU_PARENTS: dict[Submenu, Submenu] = {
    "friends_invite": "friends",
    "friends_remove": "friends",
    "help_feedback": "help",
    "wishlist_add": "wishlist",
    "wishlist_delete": "wishlist",
}

FRIEND_REMOVE_CONFIRM_TEXT = "Yes, remove friend"
WISHLIST_DELETE_CONFIRM_TEXT = "Yes, delete wish"


@dataclass
class BotState:
    add_friend_mode: set[int] = field(default_factory=set)
    remove_friend_mode: set[int] = field(default_factory=set)
    remove_friend_confirmations: dict[int, str] = field(default_factory=dict)
    friend_card_remove_confirmations: dict[int, str] = field(default_factory=dict)
    feedback_mode: set[int] = field(default_factory=set)
    delete_account_confirmation: set[int] = field(default_factory=set)
    wishlist_add_mode: set[int] = field(default_factory=set)
    wishlist_delete_confirmations: dict[int, tuple[int, int] | int] = field(default_factory=dict)
    pending_link_actions: dict[int, list[str]] = field(default_factory=dict)
    forwarded_link_collections: dict[int, list[str]] = field(default_factory=dict)
    current_submenu: dict[int, list[Submenu]] = field(default_factory=dict)

    def set_submenu(self, user_id: int, submenu: Submenu) -> None:
        if submenu == "root":
            self.current_submenu[user_id] = ["root"]
            return

        if submenu in TOP_LEVEL_MENUS:
            self.current_submenu[user_id] = ["root", submenu]
            return

        parent_menu = SUBMENU_PARENTS.get(submenu)
        if parent_menu:
            self.current_submenu[user_id] = ["root", parent_menu, submenu]
            return

        history = self.current_submenu.setdefault(user_id, ["root"])
        if history[-1] != submenu:
            history.append(submenu)

    def get_submenu(self, user_id: int) -> Submenu:
        return self.current_submenu.get(user_id, ["root"])[-1]

    def pop_submenu(self, user_id: int) -> Submenu:
        history = self.current_submenu.get(user_id, ["root"])
        if len(history) <= 1:
            self.current_submenu[user_id] = ["root"]
            return "root"

        history.pop()
        return history[-1]
