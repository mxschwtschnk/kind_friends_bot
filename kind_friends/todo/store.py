from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Task:
    id: int
    text: str
    done: bool = False


@dataclass
class ToDoList:
    owner_id: int
    title: Optional[str] = None
    tasks: List[Task] = field(default_factory=list)
    anchor_chat_id: Optional[int] = None
    anchor_message_id: Optional[int] = None
    next_task_id: int = 1


class ListStore:
    def __init__(self):
        self._lists: Dict[int, ToDoList] = {}

    def has_list(self, user_id: int) -> bool:
        return user_id in self._lists

    def create_list(self, user_id: int) -> ToDoList:
        if self.has_list(user_id):
            raise ValueError("List already exists")
        todo_list = ToDoList(owner_id=user_id)
        self._lists[user_id] = todo_list
        return todo_list

    def delete_list(self, user_id: int) -> None:
        self._lists.pop(user_id, None)

    def get_list(self, user_id: int) -> Optional[ToDoList]:
        return self._lists.get(user_id)

    def set_title(self, user_id: int, title: str) -> ToDoList:
        todo_list = self._require_list(user_id)
        todo_list.title = title
        return todo_list

    def add_task(self, user_id: int, text: str) -> Task:
        todo_list = self._require_list(user_id)
        task = Task(id=todo_list.next_task_id, text=text)
        todo_list.tasks.append(task)
        todo_list.next_task_id += 1
        return task

    def toggle_task(self, user_id: int, task_id: int) -> Task:
        task = self._get_task(user_id, task_id)
        task.done = not task.done
        return task

    def set_task_status(self, user_id: int, task_id: int, done: bool) -> Task:
        task = self._get_task(user_id, task_id)
        task.done = done
        return task

    def delete_task(self, user_id: int, task_id: int) -> None:
        todo_list = self._require_list(user_id)
        todo_list.tasks = [task for task in todo_list.tasks if task.id != task_id]

    def set_anchor(self, user_id: int, chat_id: int, message_id: int) -> None:
        todo_list = self._require_list(user_id)
        todo_list.anchor_chat_id = chat_id
        todo_list.anchor_message_id = message_id

    def get_anchor(self, user_id: int) -> Optional[tuple[int, int]]:
        todo_list = self._lists.get(user_id)
        if todo_list and todo_list.anchor_chat_id and todo_list.anchor_message_id:
            return todo_list.anchor_chat_id, todo_list.anchor_message_id
        return None

    def _require_list(self, user_id: int) -> ToDoList:
        todo_list = self.get_list(user_id)
        if not todo_list:
            raise ValueError("List does not exist")
        return todo_list

    def _get_task(self, user_id: int, task_id: int) -> Task:
        todo_list = self._require_list(user_id)
        for task in todo_list.tasks:
            if task.id == task_id:
                return task
        raise ValueError("Task not found")
