import hashlib
from collections import defaultdict
from enum import IntEnum

import cbor
import clvm
from chiasim.wallet.keychain import Keychain
from puzzles import p2_delegated_puzzle_or_hidden_puzzle as taproot
from standard_wallet.wallet import Wallet
try:
    from chialisp import *
except Exception:
    from recoverable_wallet.chialisp import *

from chiasim.validation.Conditions import ConditionOpcode
from chiasim.atoms import hexbytes
from chiasim.hashable import Program, ProgramHash, CoinSolution, SpendBundle, BLSSignature
from chiasim.hashable.CoinSolution import CoinSolutionList
from clvm_tools import binutils
from clvm import to_sexp_f
from chiasim.validation.Conditions import \
    (conditions_by_opcode, make_create_coin_condition, make_assert_my_coin_id_condition, make_assert_min_time_condition)
from chiasim.validation.consensus import\
    (conditions_for_solution, hash_key_pairs_for_conditions_dict, conditions_dict_for_solution)
from fractions import Fraction
import math

from utilities.BLSHDKey import BLSPublicHDKey


def hash_sha256(val):
    return hashlib.sha256(val).digest()


def make_solution(parent,
                  puzzlehash,
                  value,
                  stake_factor,
                  primaries=[],
                  recovery=False,
                  hidden_public_key=None,
                  hidden_puzzle=None):
    conditions = []
    for primary in primaries:
        conditions.append(make_create_coin_condition(primary['puzzlehash'], primary['amount']))
    conditions = [binutils.assemble("#q"), conditions]
    solution = [conditions, [], parent, puzzlehash, value, math.floor(value * stake_factor)]
    delegated_puzzle = Program(to_sexp_f(conditions))
    if recovery:
        solution = taproot.solution_with_hidden_puzzle(hidden_public_key, hidden_puzzle, solution)
    else:
        synthetic_public_key = taproot.calculate_synthetic_public_key(hidden_public_key,
                                                                      hidden_puzzle)
        solution = taproot.solution_with_delegated_puzzle(synthetic_public_key,
                                                          delegated_puzzle,
                                                          solution)
    program = Program(to_sexp_f(solution))
    return program


def get_destination_puzzle_hash(solution):
    conditions_dict = conditions_dict_for_solution(solution)
    val = conditions_dict.get(ConditionOpcode.CREATE_COIN, [])
    assert(len(val) == 1)
    assert(len(val[0]) == 3)
    return val[0][1]


def aggsig_condition(key):
    op_aggsig = ConditionOpcode.AGG_SIG[0]
    return make_list(quote(op_aggsig),
                     quote(f'0x{hexbytes(key)}'),
                     sha256tree(args(0)))


class InsufficientFundsError(BaseException):
    pass


class DurationType(IntEnum):
    BLOCKS = 1
    WALLCLOCK_TIME = 2


class RecoverableWallet(Wallet):
    def __init__(self, stake_factor, escrow_duration, duration_type):
        super().__init__()
        self.escrow_duration = escrow_duration
        self.duration_type = duration_type
        self.stake_factor = stake_factor
        self.backup_hd_root_public_key = self.extended_secret_key.public_hd_key()
        self.backup_private_key = self.extended_secret_key.private_child(self.next_address)
        self.next_address += 1
        self.escrow_coins = defaultdict(set)
        self.keychain = Keychain()

    def get_recovery_public_key(self):
        return self.backup_private_key.public_key()

    def get_recovery_private_key(self):
        return self.backup_private_key

    def get_recovery_hd_root_public_key(self):
        return self.backup_hd_root_public_key

    def get_escrow_duration(self):
        return self.escrow_duration

    def get_duration_type(self):
        return self.duration_type

    def get_stake_factor(self):
        return self.stake_factor

    def get_backup_string(self):
        d = dict()
        d['root_public_key'] = bytes(self.get_recovery_hd_root_public_key())
        d['secret_key'] = bytes(self.get_recovery_private_key())
        d['escrow_duration'] = self.get_escrow_duration()
        d['duration_type'] = self.get_duration_type()
        d['stake_factor'] = self.get_stake_factor().as_tuple()
        return str(hexbytes(cbor.dumps(d)))

    def get_escrow_puzzle_with_params(self, recovery_pubkey, pubkey, duration, duration_type):
        op_block_age_exceeds = ConditionOpcode.ASSERT_BLOCK_AGE_EXCEEDS[0]
        op_time_exceeds = ConditionOpcode.ASSERT_TIME_EXCEEDS[0]
        solution = args(0)
        solution_args = args(1)
        secure_switch = args(2)
        evaluate_solution = eval(solution, solution_args)
        standard_conditions = make_list(aggsig_condition(pubkey),
                                        terminator=evaluate_solution)
        if duration_type == DurationType.BLOCKS:
            op_code = op_block_age_exceeds
        elif duration_type == DurationType.WALLCLOCK_TIME:
            op_code = op_time_exceeds
        recovery_conditions = make_list(aggsig_condition(recovery_pubkey),
                                        make_list(quote(op_code),
                                                  quote(duration)),
                                        terminator=evaluate_solution)
        escrow_puzzle = make_if(is_zero(secure_switch),
                                standard_conditions,
                                recovery_conditions)
        program = Program(binutils.assemble(escrow_puzzle))
        return program

    def get_new_puzzle_with_params_and_root(self, recovery_pubkey, pubkey, stake_factor, duration, duration_type):
        escrow_conditions_program = self.get_send_to_escrow_puzzle(recovery_pubkey,
                                                                   pubkey,
                                                                   stake_factor,
                                                                   duration,
                                                                   duration_type)

        return taproot.puzzle_for_public_key_and_hidden_puzzle(recovery_pubkey,
                                                               escrow_conditions_program)

    def get_send_to_escrow_puzzle(self, recovery_pubkey, pubkey, stake_factor, duration, duration_type):
        op_create = ConditionOpcode.CREATE_COIN[0]
        op_consumed = ConditionOpcode.ASSERT_COIN_CONSUMED[0]
        solution = args(0)
        solution_args = args(1)
        parent = args(2)
        puzzle_hash = args(3)
        value = args(4)
        new_value = args(5)

        evaluate_solution = eval(solution, solution_args)
        escrow_program = self.get_escrow_puzzle_with_params(recovery_pubkey, pubkey, duration, duration_type)
        escrow_puzzlehash = f'0x' + str(hexbytes(ProgramHash(escrow_program)))
        f = Fraction(stake_factor)
        stake_factor_numerator = quote(f.numerator)
        stake_factor_denominator = quote(f.denominator)
        create_condition = make_if(equal(multiply(new_value, stake_factor_denominator),
                                         multiply(value, stake_factor_numerator)),
                                   make_list(quote(op_create), quote(escrow_puzzlehash), new_value),
                                   fail())
        coin_id = sha256(parent, puzzle_hash, value)
        consumed_condition = make_list(quote(op_consumed), coin_id)
        escrow_conditions = make_list(create_condition, consumed_condition)
        program = Program(binutils.assemble(escrow_conditions))
        return program

    def get_new_puzzle_with_params(self, pubkey, stake_factor, escrow_duration, duration_type):
        return self.get_new_puzzle_with_params_and_root(bytes(self.get_recovery_public_key()),
                                                        pubkey,
                                                        stake_factor,
                                                        escrow_duration,
                                                        duration_type)

    def get_next_public_key(self):
        pubkey = self.extended_secret_key.public_child(self.next_address)
        self.pubkey_num_lookup[bytes(pubkey)] = self.next_address
        self.next_address = self.next_address + 1

        secret_exponent = self.extended_secret_key.private_child(self.next_address).secret_exponent()
        self.keychain.add_secret_exponents([secret_exponent])

        return pubkey

    def get_new_puzzle(self):
        pubkey = bytes(self.get_next_public_key())
        program = self.get_new_puzzle_with_params(pubkey,
                                                  self.get_stake_factor(),
                                                  self.get_escrow_duration(),
                                                  self.get_duration_type())
        return program

    def get_new_puzzlehash(self):
        puzzle = self.get_new_puzzle()
        puzzlehash = ProgramHash(puzzle)
        return puzzlehash

    def can_generate_puzzle_hash(self, hash):
        return any(map(lambda child: hash == ProgramHash(self.get_new_puzzle_with_params(
            bytes(self.extended_secret_key.public_child(child)),
            self.get_stake_factor(),
            self.get_escrow_duration(),
            self.get_duration_type())),
                reversed(range(self.next_address))))

    def is_in_escrow(self, coin):
        keys = self.get_keys_for_escrow_puzzle(coin.puzzle_hash)
        return keys is not None

    def balance(self):
        return sum([coin.amount for coin in self.my_utxos])

    def notify(self, additions, deletions):
        for coin in deletions:
            if coin in self.my_utxos:
                self.my_utxos.remove(coin)
                self.current_balance -= coin.amount
            for _, coin_set in self.escrow_coins.items():
                if coin in coin_set:
                    print(f'Notice: {coin.name()} was removed from escrow by clawback')
                    coin_set.remove(coin)
        for coin in additions:
            if self.can_generate_puzzle_hash(coin.puzzle_hash):
                self.current_balance += coin.amount
                self.my_utxos.add(coin)

        self.temp_utxos = self.my_utxos.copy()
        self.temp_balance = self.current_balance

    def can_generate_puzzle_hash_with_root_public_key(self,
                                                      hash,
                                                      root_public_key_serialized,
                                                      stake_factor,
                                                      escrow_duration,
                                                      duration_type):
        root_public_key = BLSPublicHDKey.from_bytes(root_public_key_serialized)
        recovery_pubkey = bytes(root_public_key.public_child(0))
        return any(map(lambda child: hash == ProgramHash(self.get_new_puzzle_with_params_and_root(
            recovery_pubkey,
            bytes(root_public_key.public_child(child)),
            stake_factor,
            escrow_duration,
            duration_type)),
                reversed(range(20))))

    def find_pubkey_for_hash(self, hash, root_public_key_serialized, stake_factor, escrow_duration, duration_type):
        root_public_key = BLSPublicHDKey.from_bytes(root_public_key_serialized)
        recovery_pubkey = bytes(root_public_key.public_child(0))
        for child in reversed(range(20)):
            pubkey = bytes(root_public_key.public_child(child))
            puzzle = self.get_new_puzzle_with_params_and_root(recovery_pubkey,
                                                              pubkey,
                                                              stake_factor,
                                                              escrow_duration,
                                                              duration_type)
            puzzlehash = ProgramHash(puzzle)
            if hash == puzzlehash:
                return pubkey

    def get_keys(self, hash):
        for child in range(self.next_address):
            pubkey = self.extended_secret_key.public_child(child)
            if hash == ProgramHash(self.get_new_puzzle_with_params(bytes(pubkey),
                                                                   self.get_stake_factor(),
                                                                   self.get_escrow_duration(),
                                                                   self.get_duration_type())):
                return pubkey, self.extended_secret_key.private_child(child)

    def generate_unsigned_transaction(self, amount, newpuzzlehash):
        stake_factor = self.get_stake_factor()
        utxos = self.select_coins(amount)
        if utxos is None:
            raise InsufficientFundsError
        coin_solutions = []
        output_id = None
        spend_value = sum([coin.amount for coin in utxos])
        change = spend_value - amount
        for coin in utxos:
            puzzle_hash = coin.puzzle_hash

            pubkey, secretkey = self.get_keys(puzzle_hash)
            hidden_public_key = bytes(self.get_recovery_public_key())
            hidden_puzzle = self.get_send_to_escrow_puzzle(hidden_public_key,
                                                           pubkey,
                                                           stake_factor,
                                                           self.get_escrow_duration(),
                                                           self.get_duration_type())
            hidden_puzzle_hash = ProgramHash(hidden_puzzle)
            synthetic_offset = taproot.calculate_synthetic_offset(hidden_public_key, hidden_puzzle_hash)
            secret_exponent = self.extended_secret_key.private_child(0).secret_exponent()
            synthetic_secret_exponent = secret_exponent + synthetic_offset
            self.keychain.add_secret_exponents([synthetic_secret_exponent])

            if output_id is None:
                primaries = [{'puzzlehash': newpuzzlehash, 'amount': amount}]
                if change > 0:
                    changepuzzlehash = self.get_new_puzzlehash()
                    primaries.append({'puzzlehash': changepuzzlehash, 'amount': change})
                solution = make_solution(coin.parent_coin_info, coin.puzzle_hash, coin.amount, stake_factor,
                                         hidden_public_key=hidden_public_key,
                                         hidden_puzzle=hidden_puzzle,
                                         primaries=primaries)
                output_id = hash_sha256(coin.name() + newpuzzlehash)
            else:
                solution = make_solution(coin.parent_coin_info, coin.puzzle_hash, coin.amount, stake_factor,
                                         hidden_public_key=hidden_public_key,
                                         hidden_puzzle=hidden_puzzle)
            coin_solutions.append(CoinSolution(coin, solution))
        return coin_solutions

    def generate_unsigned_transaction_without_recipient(self, amount):
        stake_factor = self.get_stake_factor()
        utxos = self.select_coins(amount)
        if utxos is None:
            raise InsufficientFundsError
        coin_solutions = []
        output_id = None
        spend_value = sum([coin.amount for coin in utxos])
        change = spend_value - amount
        for coin in utxos:
            puzzle_hash = coin.puzzle_hash

            pubkey, secretkey = self.get_keys(puzzle_hash)
            hidden_public_key = bytes(self.get_recovery_public_key())
            hidden_puzzle = self.get_send_to_escrow_puzzle(hidden_public_key,
                                                           pubkey,
                                                           stake_factor,
                                                           self.get_escrow_duration(),
                                                           self.get_duration_type())
            hidden_puzzle_hash = ProgramHash(hidden_puzzle)
            synthetic_offset = taproot.calculate_synthetic_offset(hidden_public_key, hidden_puzzle_hash)
            secret_exponent = self.extended_secret_key.private_child(0).secret_exponent()
            synthetic_secret_exponent = secret_exponent + synthetic_offset
            self.keychain.add_secret_exponents([synthetic_secret_exponent])

            if output_id is None:
                primaries = []
                if change > 0:
                    changepuzzlehash = self.get_new_puzzlehash()
                    primaries.append({'puzzlehash': changepuzzlehash, 'amount': change})
                solution = make_solution(coin.parent_coin_info, coin.puzzle_hash, coin.amount, stake_factor,
                                         hidden_public_key=hidden_public_key,
                                         hidden_puzzle=hidden_puzzle,
                                         primaries=primaries)
                output_id = True
            else:
                solution = make_solution(coin.parent_coin_info, coin.puzzle_hash, coin.amount, stake_factor)
            coin_solutions.append(CoinSolution(coin, solution))
        return coin_solutions

    def generate_recovery_to_escrow_transaction(self,
                                                coin,
                                                recovery_pubkey,
                                                pubkey,
                                                stake_factor,
                                                escrow_duration,
                                                duration_type):
        hidden_puzzle = self.get_send_to_escrow_puzzle(recovery_pubkey,
                                                       pubkey,
                                                       stake_factor,
                                                       escrow_duration,
                                                       duration_type)

        solution = make_solution(coin.parent_coin_info,
                                 coin.puzzle_hash,
                                 coin.amount,
                                 stake_factor,
                                 recovery=True,
                                 hidden_public_key=recovery_pubkey,
                                 hidden_puzzle=hidden_puzzle)

        secret_exponent = self.get_recovery_private_key().secret_exponent()
        self.keychain.add_secret_exponents([secret_exponent])
        destination_puzzle_hash = get_destination_puzzle_hash(solution)
        staked_amount = math.ceil(coin.amount * (stake_factor - 1))
        coin_solutions = self.generate_unsigned_transaction_without_recipient(staked_amount)
        coin_solutions.append(CoinSolution(coin, solution))
        return coin_solutions, destination_puzzle_hash, coin.amount + staked_amount

    def generate_signed_recovery_to_escrow_transaction(self,
                                                       coin,
                                                       recovery_pubkey,
                                                       pubkey,
                                                       stake_factor,
                                                       escrow_duration,
                                                       duration_type):
        transaction, destination_puzzlehash, amount = self.generate_recovery_to_escrow_transaction(coin,
                                                                                                   recovery_pubkey,
                                                                                                   pubkey,
                                                                                                   stake_factor,
                                                                                                   escrow_duration,
                                                                                                   duration_type)
        signed_transaction = self.sign_transaction(transaction)
        return signed_transaction, destination_puzzlehash, amount

    def sign_transaction(self, coin_solutions: CoinSolutionList):
        signatures = []
        for coin_solution in coin_solutions:
            signature = self.keychain.signature_for_solution(coin_solution.solution)
            signatures.append(signature)
        aggsig = BLSSignature.aggregate(signatures)
        spend_bundle = SpendBundle(CoinSolutionList(coin_solutions), aggsig)
        return spend_bundle

    def get_keys_for_escrow_puzzle(self, hash):
        for child in range(self.next_address):
            pubkey = self.extended_secret_key.public_child(child)
            escrow_hash = ProgramHash(self.get_escrow_puzzle_with_params(bytes(self.get_recovery_public_key()),
                                                                         bytes(pubkey),
                                                                         self.get_escrow_duration(),
                                                                         self.get_duration_type()))
            if hash == escrow_hash:
                return pubkey, self.extended_secret_key.private_child(child)

    def generate_signed_transaction(self, amount, newpuzzlehash):
        coin_solutions = self.generate_unsigned_transaction(amount, newpuzzlehash)
        if coin_solutions is None:
            return None
        return self.sign_transaction(coin_solutions)

    def generate_clawback_transaction(self, coins):
        signatures = []
        coin_solutions = []
        for coin in coins:
            pubkey, secret_key = self.get_keys_for_escrow_puzzle(coin.puzzle_hash)
            puzzle = self.get_escrow_puzzle_with_params(bytes(self.get_recovery_public_key()),
                                                        bytes(pubkey),
                                                        self.get_escrow_duration(),
                                                        self.get_duration_type())

            op_create_coin = ConditionOpcode.CREATE_COIN[0]
            puzzlehash = f'0x' + str(hexbytes(self.get_new_puzzlehash()))
            solution_src = sexp(quote(sexp(sexp(op_create_coin, puzzlehash, coin.amount))), sexp(), 0)
            solution = Program(binutils.assemble(solution_src))

            puzzle_solution_list = clvm.to_sexp_f([puzzle, solution])
            coin_solution = CoinSolution(coin, puzzle_solution_list)
            coin_solutions.append(coin_solution)

            conditions_dict = conditions_by_opcode(conditions_for_solution(puzzle_solution_list))
            for _ in hash_key_pairs_for_conditions_dict(conditions_dict):
                signature = secret_key.sign(_.message_hash)
                signatures.append(signature)

        coin_solution_list = CoinSolutionList(coin_solutions)
        aggsig = BLSSignature.aggregate(signatures)
        spend_bundle = SpendBundle(coin_solution_list, aggsig)
        return spend_bundle

    def find_pubkey_for_escrow_puzzle(self, coin, root_public_key, duration, duration_type):
        recovery_pubkey = bytes(root_public_key.public_child(0))

        child = 0
        while True:
            pubkey = root_public_key.public_child(child)
            test_hash = ProgramHash(self.get_escrow_puzzle_with_params(recovery_pubkey,
                                                                       bytes(pubkey),
                                                                       duration,
                                                                       duration_type))
            if coin.puzzle_hash == test_hash:
                return pubkey
            child += 1

    def generate_recovery_transaction(self, coins, root_public_key, secret_key, escrow_duration, duration_type):
        recovery_pubkey = bytes(root_public_key.public_child(0))
        signatures = []
        coin_solutions = []
        for coin in coins:
            pubkey = self.find_pubkey_for_escrow_puzzle(coin, root_public_key, escrow_duration, duration_type)
            puzzle = self.get_escrow_puzzle_with_params(recovery_pubkey, bytes(pubkey), escrow_duration, duration_type)

            op_create_coin = ConditionOpcode.CREATE_COIN[0]
            puzzlehash = f'0x' + str(hexbytes(self.get_new_puzzlehash()))
            solution_src = sexp(quote(sexp(sexp(op_create_coin, puzzlehash, coin.amount))), sexp(), 1)
            solution = Program(binutils.assemble(solution_src))

            puzzle_solution_list = clvm.to_sexp_f([puzzle, solution])
            coin_solution = CoinSolution(coin, puzzle_solution_list)
            coin_solutions.append(coin_solution)

            conditions_dict = conditions_by_opcode(conditions_for_solution(puzzle_solution_list))
            for _ in hash_key_pairs_for_conditions_dict(conditions_dict):
                signature = secret_key.sign(_.message_hash)
                signatures.append(signature)

        coin_solution_list = CoinSolutionList(coin_solutions)
        aggsig = BLSSignature.aggregate(signatures)
        spend_bundle = SpendBundle(coin_solution_list, aggsig)
        return spend_bundle

"""
Copyright 2018 Chia Network Inc
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
   http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""