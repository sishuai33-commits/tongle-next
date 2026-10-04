import unittest

from tongle.model_refs import decode_references, encode_references


A = 'a' * 32
B = '0123456789abcdef0123456789abcdef'
SHA = 'SHA256HASH-not-an-id-body_sha256'
C = 'deadBEEF' + '0' * 24  # not 32 lowercase hex (uppercase) -> untouched


def _aliases(reverse):
    return {real: alias for alias, real in reverse.items()}


class ModelRefsTests(unittest.TestCase):
    def test_round_trip_restores_real_ids(self):
        payload = {'existing': {'cards': [{'id': A, 'version': 2},
                                          {'card_id': B, 'version': 1}]},
                   'sources': [{'source_id': A, 'quote': '原文'}]}
        encoded, reverse = encode_references(payload)
        self.assertNotIn(A, str(encoded))
        self.assertNotIn(B, str(encoded))
        self.assertEqual(decode_references(encoded, reverse), payload)

    def test_same_id_reuses_one_alias_across_payload(self):
        payload = {'existing': {'cards': [{'id': A}, {'card_id': A}]},
                   'operations': [{'op': 'update', 'id': A}],
                   'sources': [{'source_id': A}]}
        encoded, reverse = encode_references(payload)
        self.assertEqual(len(reverse), 1)
        alias = next(iter(reverse))
        self.assertEqual(reverse[alias], A)
        flat = str(encoded)
        self.assertEqual(flat.count(alias), 4)
        self.assertNotIn(A, flat)

    def test_nested_human_checks_and_evidence_are_mapped(self):
        payload = {'required_human_checks': [{'id': A, 'version': 2, 'body_sha256': SHA}],
                   'proposal': {'operations': [
                       {'op': 'update', 'id': A, 'evidence': [{'source_id': B, 'quote': 'q', 'version': 3}]}]}}
        encoded, reverse = encode_references(payload)
        chk = encoded['required_human_checks'][0]
        self.assertNotEqual(chk['id'], A)
        self.assertEqual(chk['body_sha256'], SHA)
        ev = encoded['proposal']['operations'][0]['evidence'][0]
        self.assertNotEqual(ev['source_id'], B)
        self.assertEqual(ev['quote'], 'q')
        self.assertEqual(decode_references(encoded, reverse), payload)

    def test_body_quote_sha_and_other_keys_unchanged(self):
        payload = {'existing': {'cards': [{'id': A, 'body': '包含 ' + B + ' 的正文',
                                           'body_sha256': B, 'path': B}]},
                   'sources': [{'source_id': A, 'text': B, 'quote': B}]}
        encoded, reverse = encode_references(payload)
        self.assertEqual(encoded['existing']['cards'][0]['body'], '包含 ' + B + ' 的正文')
        self.assertEqual(encoded['existing']['cards'][0]['body_sha256'], B)
        self.assertEqual(encoded['existing']['cards'][0]['path'], B)
        self.assertEqual(encoded['sources'][0]['text'], B)
        self.assertEqual(encoded['sources'][0]['quote'], B)

    def test_only_lowercase_32_hex_identity_values_are_aliased(self):
        payload = {'id': C, 'version': 2, 'output_source': 'short',
                   'receipt_id': A.upper(), 'reason': A}
        encoded, reverse = encode_references(payload)
        self.assertEqual(encoded, payload)
        self.assertEqual(reverse, {})

    def test_encode_does_not_mutate_payload_in_place(self):
        payload = {'existing': {'cards': [{'id': A, 'body': 'x'}]},
                   'sources': [{'source_id': B}]}
        snapshot = {'existing': {'cards': [{'id': A, 'body': 'x'}]},
                    'sources': [{'source_id': B}]}
        encoded, _ = encode_references(payload)
        self.assertEqual(payload, snapshot)
        self.assertIsNot(encoded['existing'], payload['existing'])
        self.assertIsNot(encoded['sources'], payload['sources'])

    def test_unknown_alias_is_not_corrected_or_fuzzy_matched(self):
        _, reverse = encode_references({'id': A})
        alias = next(iter(reverse))
        # A near-miss alias, an alias under a non-identity key, and a real id
        # are all left exactly as returned.
        response = {'human_checks': [{'id': alias[:-1] + '9'},
                                     {'card_id': alias.upper()},
                                     {'id': 'e' * 32}],
                    'note': alias}
        decoded = decode_references(response, reverse)
        self.assertEqual(decoded['human_checks'][0]['id'], alias[:-1] + '9')
        self.assertEqual(decoded['human_checks'][1]['card_id'], alias.upper())
        self.assertEqual(decoded['human_checks'][2]['id'], 'e' * 32)
        self.assertEqual(decoded['note'], alias)

    def test_decode_restores_known_alias_under_identity_key(self):
        _, reverse = encode_references({'card_id': A})
        alias = next(iter(reverse))
        decoded = decode_references({'human_checks': [{'id': alias, 'body_sha256': SHA}]}, reverse)
        self.assertEqual(decoded['human_checks'][0]['id'], A)
        self.assertEqual(decoded['human_checks'][0]['body_sha256'], SHA)

    def test_decode_without_map_is_identity(self):
        response = {'id': 'ref001'}
        self.assertEqual(decode_references(response, {}), response)


class TypedAliasTests(unittest.TestCase):
    def test_card_source_receipt_aliases_are_typed_disjoint_and_consistent(self):
        source = '1' * 32
        card = '2' * 32
        receipt = '3' * 32
        payload = {
            'receipt_id': receipt,
            'existing': {'cards': [{'id': card}]},
            'sources': [{'source_id': source, 'quote': 'q'}],
            'required_human_checks': [{'id': card, 'body_sha256': SHA}],
            'proposal': {'operations': [{'op': 'update', 'id': card,
                'evidence': [{'source_id': source, 'quote': 'q'}]}]},
            'needs_cards': [source, card],
        }
        encoded, reverse = encode_references(payload)
        alias = _aliases(reverse)
        # Typed by role.
        self.assertTrue(alias[source].startswith('src'))
        self.assertTrue(alias[card].startswith('card'))
        self.assertTrue(alias[receipt].startswith('receipt'))
        # Disjoint aliases even though each is the first of its type.
        self.assertEqual(len({alias[source], alias[card], alias[receipt]}), 3)
        # The same real id is consistent in every location.
        self.assertEqual(encoded['sources'][0]['source_id'], alias[source])
        self.assertEqual(encoded['proposal']['operations'][0]['evidence'][0]['source_id'], alias[source])
        self.assertEqual(encoded['existing']['cards'][0]['id'], alias[card])
        self.assertEqual(encoded['required_human_checks'][0]['id'], alias[card])
        self.assertEqual(encoded['receipt_id'], alias[receipt])
        # needs_cards classifies each bare value by its real role, not by the
        # request key, so the source stays a source alias.
        self.assertEqual(encoded['needs_cards'], [alias[source], alias[card]])
        self.assertEqual(decode_references(encoded, reverse), payload)

    def test_same_id_ambiguous_source_and_card_is_stable_generic_ref(self):
        both = 'd' * 32
        payload = {'existing': {'cards': [{'id': both}]},
                   'sources': [{'source_id': both, 'quote': 'q'}],
                   'needs_cards': [both]}
        encoded, reverse = encode_references(payload)
        alias = _aliases(reverse)
        self.assertTrue(alias[both].startswith('ref'))
        self.assertEqual(encoded['existing']['cards'][0]['id'], alias[both])
        self.assertEqual(encoded['sources'][0]['source_id'], alias[both])
        self.assertEqual(encoded['needs_cards'], [alias[both]])
        # Deterministic: the same input yields the same alias.
        encoded2, reverse2 = encode_references(payload)
        self.assertEqual(encoded, encoded2)
        self.assertEqual(reverse, reverse2)
        self.assertEqual(decode_references(encoded, reverse), payload)

    def test_literal_card_and_source_alias_values_are_not_shadowed(self):
        card = 'a' * 32
        source = 'b' * 32
        payload = {'existing': {'cards': [{'id': card}, {'id': 'card001'}]},
                   'sources': [{'source_id': source}, {'source_id': 'src001'}]}
        encoded, reverse = encode_references(payload)
        alias = _aliases(reverse)
        # Generated aliases skip the occupied literal strings.
        self.assertEqual(alias[card], 'card002')
        self.assertEqual(alias[source], 'src002')
        self.assertEqual(encoded['existing']['cards'][1]['id'], 'card001')
        self.assertEqual(encoded['sources'][1]['source_id'], 'src001')
        # Decode must not reinterpret the literal strings as aliases.
        self.assertEqual(decode_references(encoded, reverse), payload)

    def test_source_alias_via_needs_cards_decodes_to_source_and_is_not_card(self):
        source = 'c' * 32
        card = 'e' * 32
        payload = {'existing': {'cards': [{'id': card}]},
                   'sources': [{'source_id': source}]}
        encoded, reverse = encode_references(payload)
        alias = _aliases(reverse)
        self.assertTrue(alias[source].startswith('src'))
        # Model asks for the source alias through the card-read list.
        decoded = decode_references({'needs_cards': [alias[source]]}, reverse)
        self.assertEqual(decoded['needs_cards'], [source])
        # The catalog gate only holds card ids, so this is rejected as foreign.
        catalog_card_ids = {card}
        self.assertNotIn(decoded['needs_cards'][0], catalog_card_ids)


if __name__ == '__main__':
    unittest.main()


class AliasBoundaryTests(unittest.TestCase):
    def test_existing_alias_value_is_not_reinterpreted(self):
        payload={'cards':[{'id':'ref001'},{'id':'a'*32}]}
        encoded,reverse=encode_references(payload)
        self.assertNotEqual(encoded['cards'][0]['id'],encoded['cards'][1]['id'])
        self.assertEqual(decode_references(encoded,reverse),payload)
    def test_trailing_newline_is_not_a_hex_identity(self):
        payload={'id':'a'*32+'\n'}
        encoded,reverse=encode_references(payload)
        self.assertEqual(encoded,payload)
        self.assertEqual(reverse,{})
