from __future__ import annotations

import unittest

from cheby_gateway.service import GatewayError

from .helpers import make_context, pair


class MessagePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = make_context()
        await self.context.service.start()
        _, self.device = pair(self.context)
        self.thread = await self.context.service.create_thread(
            self.device,
            "Bounded history",
        )
        self.raw_thread_id = self.context.store.thread_raw_id(self.thread.id)

    async def asyncTearDown(self) -> None:
        await self.context.service.close()
        self.context.close()

    def _append_turn(self, index: int, text: str | None = None) -> None:
        content = text if text is not None else "question-%02d" % index
        self.context.bridge.threads[self.raw_thread_id]["turns"].append(
            {
                "id": "raw-turn-page-%03d" % index,
                "status": "completed",
                "startedAt": 1_784_419_200 + index,
                "completedAt": 1_784_419_200 + index,
                "items": [
                    {
                        "id": "raw-user-page-%03d" % index,
                        "type": "userMessage",
                        "clientId": "client-page-%03d" % index,
                        "content": [{"type": "text", "text": content}],
                    },
                    {
                        "id": "raw-agent-page-%03d" % index,
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "answer-%02d %s" % (index, content),
                    },
                ],
            }
        )

    @staticmethod
    def _ids(detail) -> list[str]:
        return [str(message["messageId"]) for message in detail.messages]

    async def test_default_is_safe_recent_page_and_cursor_is_stable(self) -> None:
        for index in range(15):
            self._append_turn(index)

        first = await self.context.service.read_thread(self.thread.id, self.device)
        self.assertEqual(20, len(first.messages))
        self.assertTrue(first.has_more_messages)
        self.assertIsNotNone(first.next_message_cursor)
        self.assertIn("question-05", first.messages[0]["fallback"]["text"])
        self.assertIn("answer-14", first.messages[-1]["fallback"]["text"])

        first_ids = self._ids(first)
        self._append_turn(15)
        second = await self.context.service.read_thread(
            self.thread.id,
            self.device,
            message_cursor=first.next_message_cursor,
        )
        second_ids = self._ids(second)
        self.assertEqual(10, len(second.messages))
        self.assertFalse(first_ids[0] in second_ids)
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assertIn("question-00", second.messages[0]["fallback"]["text"])
        self.assertIn("answer-04", second.messages[-1]["fallback"]["text"])

    async def test_cursor_is_thread_bound_and_tamper_evident(self) -> None:
        for index in range(6):
            self._append_turn(index)
        page = await self.context.service.read_thread(
            self.thread.id,
            self.device,
            message_limit=4,
        )
        cursor = page.next_message_cursor
        self.assertIsNotNone(cursor)
        with self.assertRaises(GatewayError) as tampered:
            await self.context.service.read_thread(
                self.thread.id,
                self.device,
                message_cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
            )
        self.assertEqual("MESSAGE_CURSOR_INVALID", tampered.exception.code)

        other = await self.context.service.create_thread(self.device, "Other")
        with self.assertRaises(GatewayError) as wrong_thread:
            await self.context.service.read_thread(
                other.id,
                self.device,
                message_cursor=cursor,
            )
        self.assertEqual("MESSAGE_CURSOR_INVALID", wrong_thread.exception.code)

    async def test_public_response_never_exceeds_four_mib_budget(self) -> None:
        large = "汉" * 40_000
        for index in range(14):
            self._append_turn(index, large + str(index))
        detail = await self.context.service.read_thread(self.thread.id, self.device)
        encoded = detail.model_dump_json(by_alias=True).encode("utf-8")
        self.assertLessEqual(len(encoded), self.context.settings.thread_detail_max_bytes)
        self.assertTrue(detail.has_more_messages)
        self.assertGreater(len(detail.messages), 0)

    async def test_store_defense_never_materializes_more_than_one_hundred(self) -> None:
        for index in range(60):
            self._append_turn(index)
        await self.context.service.read_thread(self.thread.id, self.device)
        snapshot = self.context.store.read_thread_recovery_snapshot(
            self.device.id,
            self.thread.id,
            message_limit=1_000_000,
        )
        self.assertEqual(100, len(snapshot.messages))
        self.assertTrue(snapshot.has_more_messages)


if __name__ == "__main__":
    unittest.main()
