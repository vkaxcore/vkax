#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2014-2022 The Dash Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Base class for RPC testing."""

import configparser
import copy
from enum import Enum
import logging
import argparse
import os
import pdb
import random
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from .authproxy import JSONRPCException
from test_framework.blocktools import TIME_GENESIS_BLOCK
from . import coverage
from .messages import (
    CTransaction,
    FromHex,
    hash256,
    msg_islock,
    msg_isdlock,
    ser_compact_size,
    ser_string,
)
from .test_node import TestNode
from .mininode import NetworkThread
from .util import (
    PortSeed,
    MAX_NODES,
    assert_equal,
    check_json_precision,
    connect_nodes,
    copy_datadir,
    disconnect_nodes,
    force_finish_mnsync,
    get_datadir_path,
    hex_str_to_bytes,
    initialize_datadir,
    p2p_port,
    set_node_times,
    set_timeout_scale,
    satoshi_round,
    sync_blocks,
    sync_mempools,
    wait_until,
    get_chain_folder,
)

class TestStatus(Enum):
    PASSED = 1
    FAILED = 2
    SKIPPED = 3

TEST_EXIT_PASSED = 0
TEST_EXIT_FAILED = 1
TEST_EXIT_SKIPPED = 77

TMPDIR_PREFIX = "dash_func_test_"

class SkipTest(Exception):
    """This exception is raised to skip a test"""

    def __init__(self, message):
        self.message = message


class BitcoinTestMetaClass(type):
    """Metaclass for BitcoinTestFramework.

    Ensures that any attempt to register a subclass of `BitcoinTestFramework`
    adheres to a standard whereby the subclass overrides `set_test_params` and
    `run_test` but DOES NOT override either `__init__` or `main`. If any of
    those standards are violated, a ``TypeError`` is raised."""

    def __new__(cls, clsname, bases, dct):
        if not clsname == 'BitcoinTestFramework':
            if not ('run_test' in dct and 'set_test_params' in dct):
                raise TypeError("BitcoinTestFramework subclasses must override "
                                "'run_test' and 'set_test_params'")
            if '__init__' in dct or 'main' in dct:
                raise TypeError("BitcoinTestFramework subclasses may not override "
                                "'__init__' or 'main'")

        return super().__new__(cls, clsname, bases, dct)


class BitcoinTestFramework(metaclass=BitcoinTestMetaClass):
    """Base class for a bitcoin test script.

    Individual bitcoin test scripts should subclass this class and override the set_test_params() and run_test() methods.

    Individual tests can also override the following methods to customize the test setup:

    - add_options()
    - setup_chain()
    - setup_network()
    - setup_nodes()

    The __init__() and main() methods should not be overridden.

    This class also contains various public and private helper methods."""

    def __init__(self):
        """Sets test framework defaults. Do not override this method. Instead, override the set_test_params() method"""
        self.chain = 'regtest'
        self.setup_clean_chain = False
        self.nodes = []
        self.network_thread = None
        self.mocktime = 0
        self.rpc_timeout = 60  # Wait for up to 60 seconds for the RPC server to respond
        self.supports_cli = False
        self.bind_to_localhost_only = True
        self.extra_args_from_options = []
        self.set_test_params()

        assert hasattr(self, "num_nodes"), "Test must set self.num_nodes in set_test_params()"

    def main(self):
        """Main function. This should not be overridden by the subclass test scripts."""

        parser = argparse.ArgumentParser(usage="%(prog)s [options]")
        parser.add_argument("--nocleanup", dest="nocleanup", default=False, action="store_true",
                            help="Leave dashds and test.* datadir on exit or error")
        parser.add_argument("--noshutdown", dest="noshutdown", default=False, action="store_true",
                            help="Don't stop dashds after the test execution")
        parser.add_argument("--cachedir", dest="cachedir", default=os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + "/../../cache"),
                            help="Directory for caching pregenerated datadirs (default: %(default)s)")
        parser.add_argument("--tmpdir", dest="tmpdir", help="Root directory for datadirs")
        parser.add_argument("-l", "--loglevel", dest="loglevel", default="INFO",
                            help="log events at this level and higher to the console. Can be set to DEBUG, INFO, WARNING, ERROR or CRITICAL. Passing --loglevel DEBUG will output all logs to console. Note that logs at all levels are always written to the test_framework.log file in the temporary test directory.")
        parser.add_argument("--tracerpc", dest="trace_rpc", default=False, action="store_true",
                            help="Print out all RPC calls as they are made")
        parser.add_argument("--portseed", dest="port_seed", default=os.getpid(), type=int,
                            help="The seed to use for assigning port numbers (default: current process id)")
        parser.add_argument("--coveragedir", dest="coveragedir",
                            help="Write tested RPC commands into this directory")
        parser.add_argument("--configfile", dest="configfile",
                            default=os.path.abspath(os.path.dirname(os.path.realpath(__file__)) + "/../../config.ini"),
                            help="Location of the test framework config file (default: %(default)s)")
        parser.add_argument("--pdbonfailure", dest="pdbonfailure", default=False, action="store_true",
                            help="Attach a python debugger if test fails")
        parser.add_argument("--usecli", dest="usecli", default=False, action="store_true",
                            help="use dash-cli instead of RPC for all commands")
        parser.add_argument("--dashd-arg", dest="dashd_extra_args", default=[], action="append",
                            help="Pass extra args to all dashd instances")
        parser.add_argument("--timeoutscale", dest="timeout_scale", default=1, type=int,
                            help="Scale the test timeouts by multiplying them with the here provided value (default: %(default)s)")
        parser.add_argument("--perf", dest="perf", default=False, action="store_true",
                            help="profile running nodes with perf for the duration of the test")
        parser.add_argument("--valgrind", dest="valgrind", default=False, action="store_true",
                            help="run nodes under the valgrind memory error detector: expect at least a ~10x slowdown, valgrind 3.14 or later required")
        parser.add_argument("--randomseed", type=int,
                            help="set a random seed for deterministically reproducing a previous test run")
        self.add_options(parser)
        # Running TestShell in a Jupyter notebook causes an additional -f argument
        # To keep TestShell from failing with an "unrecognized argument" error, we add a dummy "-f" argument
        # source: https://stackoverflow.com/questions/48796169/how-to-fix-ipykernel-launcher-py-error-unrecognized-arguments-in-jupyter/56349168#56349168
        parser.add_argument("-f", "--fff", help="a dummy argument to fool ipython", default="1")
        self.options = parser.parse_args()

        if self.options.timeout_scale < 1:
            raise RuntimeError("--timeoutscale can't be less than 1")

        set_timeout_scale(self.options.timeout_scale)

        PortSeed.n = self.options.port_seed

        check_json_precision()

        self.options.cachedir = os.path.abspath(self.options.cachedir)

        config = configparser.ConfigParser()
        config.read_file(open(self.options.configfile))
        self.config = config
        self.options.bitcoind = os.getenv("BITCOIND", default=config["environment"]["BUILDDIR"] + '/src/dashd' + config["environment"]["EXEEXT"])
        self.options.bitcoincli = os.getenv("BITCOINCLI", default=config["environment"]["BUILDDIR"] + '/src/dash-cli' + config["environment"]["EXEEXT"])

        self.extra_args_from_options = self.options.dashd_extra_args

        os.environ['PATH'] = os.pathsep.join([
            os.path.join(config['environment']['BUILDDIR'], 'src'),
            os.path.join(config['environment']['BUILDDIR'], 'src', 'qt'),
            os.environ['PATH']
        ])

        # Set up temp directory and start logging
        if self.options.tmpdir:
            self.options.tmpdir = os.path.abspath(self.options.tmpdir)
            os.makedirs(self.options.tmpdir, exist_ok=False)
        else:
            self.options.tmpdir = tempfile.mkdtemp(prefix=TMPDIR_PREFIX)
        self._start_logging()

        # Seed the PRNG. Note that test runs are reproducible if and only if
        # a single thread accesses the PRNG. For more information, see
        # https://docs.python.org/3/library/random.html#notes-on-reproducibility.
        # The network thread shouldn't access random. If we need to change the
        # network thread to access randomness, it should instantiate its own
        # random.Random object.
        seed = self.options.randomseed

        if seed is None:
            seed = random.randrange(sys.maxsize)
        else:
            self.log.debug("User supplied random seed {}".format(seed))

        random.seed(seed)
        self.log.debug("PRNG seed is: {}".format(seed))

        self.log.debug('Setting up network thread')
        self.network_thread = NetworkThread()
        self.network_thread.start()

        success = TestStatus.FAILED

        try:
            if self.options.usecli:
                if not self.supports_cli:
                    raise SkipTest("--usecli specified but test does not support using CLI")
                self.skip_if_no_cli()
            self.skip_test_if_missing_module()
            self.setup_chain()
            self.setup_network()
            self.import_deterministic_coinbase_privkeys()
            self.run_test()
            success = TestStatus.PASSED
        except JSONRPCException:
            self.log.exception("JSONRPC error")
        except SkipTest as e:
            self.log.warning("Test Skipped: %s" % e.message)
            success = TestStatus.SKIPPED
        except AssertionError:
            self.log.exception("Assertion failed")
        except KeyError:
            self.log.exception("Key error")
        except Exception:
            self.log.exception("Unexpected exception caught during testing")
        except KeyboardInterrupt:
            self.log.warning("Exiting after keyboard interrupt")

        if success == TestStatus.FAILED and self.options.pdbonfailure:
            print("Testcase failed. Attaching python debugger. Enter ? for help")
            pdb.set_trace()

        self.log.debug('Closing down network thread')
        self.network_thread.close()
        if not self.options.noshutdown:
            self.log.info("Stopping nodes")
            try:
                if self.nodes:
                    self.stop_nodes()
            except BaseException:
                success = False
                self.log.exception("Unexpected exception caught during shutdown")
        else:
            for node in self.nodes:
                node.cleanup_on_exit = False
            self.log.info("Note: dashds were not stopped and may still be running")

        should_clean_up = (
            not self.options.nocleanup and
            not self.options.noshutdown and
            success != TestStatus.FAILED and
            not self.options.perf
        )
        if should_clean_up:
            self.log.info("Cleaning up {} on exit".format(self.options.tmpdir))
            cleanup_tree_on_exit = True
        elif self.options.perf:
            self.log.warning("Not cleaning up dir {} due to perf data".format(self.options.tmpdir))
            cleanup_tree_on_exit = False
        else:
            self.log.warning("Not cleaning up dir {}".format(self.options.tmpdir))
            cleanup_tree_on_exit = False

        if success == TestStatus.PASSED:
            self.log.info("Tests successful")
            exit_code = TEST_EXIT_PASSED
        elif success == TestStatus.SKIPPED:
            self.log.info("Test skipped")
            exit_code = TEST_EXIT_SKIPPED
        else:
            self.log.error("Test failed. Test logging available at %s/test_framework.log", self.options.tmpdir)
            self.log.error("")
            self.log.error("Hint: Call {} '{}' to consolidate all logs".format(os.path.normpath(os.path.dirname(os.path.realpath(__file__)) + "/../combine_logs.py"), self.options.tmpdir))
            self.log.error("")
            self.log.error("If this failure happened unexpectedly or intermittently, please file a bug and provide a link or upload of the combined log.")
            self.log.error(self.config['environment']['PACKAGE_BUGREPORT'])
            self.log.error("")
            exit_code = TEST_EXIT_FAILED
        logging.shutdown()
        if cleanup_tree_on_exit:
            shutil.rmtree(self.options.tmpdir)
        sys.exit(exit_code)

    # Methods to override in subclass test scripts.
    def set_test_params(self):
        """Tests must override this method to change default values for number of nodes, topology, etc"""
        raise NotImplementedError

    def add_options(self, parser):
        """Override this method to add command-line options to the test"""
        pass

    def skip_test_if_missing_module(self):
        """Override this method to skip a test if a module is not compiled"""
        pass

    def setup_chain(self):
        """Override this method to customize blockchain setup"""
        self.log.info("Initializing test directory " + self.options.tmpdir)
        if self.setup_clean_chain:
            self._initialize_chain_clean()
            self.set_genesis_mocktime()
        else:
            self._initialize_chain()
            self.set_cache_mocktime()

    def setup_network(self):
        """Override this method to customize test network topology"""
        self.setup_nodes()

        # Connect the nodes as a "chain".  This allows us
        # to split the network between nodes 1 and 2 to get
        # two halves that can work on competing chains.
        #
        # Topology looks like this:
        # node0 <-- node1 <-- node2 <-- node3
        #
        # If all nodes are in IBD (clean chain from genesis), node0 is assumed to be the source of blocks (miner). To
        # ensure block propagation, all nodes will establish outgoing connections toward node0.
        # See fPreferredDownload in net_processing.
        #
        # If further outbound connections are needed, they can be added at the beginning of the test with e.g.
        # connect_nodes(self.nodes[1], 2)
        for i in range(self.num_nodes - 1):
            connect_nodes(self.nodes[i + 1], i)
        self.sync_all()

    def setup_nodes(self):
        """Override this method to customize test node setup"""
        extra_args = None
        if hasattr(self, "extra_args"):
            extra_args = self.extra_args
        self.add_nodes(self.num_nodes, extra_args)
        self.start_nodes()

    def import_deterministic_coinbase_privkeys(self):
        if self.setup_clean_chain:
            return

        for n in self.nodes:
            try:
                n.getwalletinfo()
            except JSONRPCException as e:
                assert str(e).startswith('Method not found')
                continue

            n.importprivkey(n.get_deterministic_priv_key().key)

    def run_test(self):
        """Tests must override this method to define test logic"""
        raise NotImplementedError

    # Public helper methods. These can be accessed by the subclass test scripts.

    def add_nodes(self, num_nodes, extra_args=None, *, rpchost=None, binary=None):
        """Instantiate TestNode objects.

        Should only be called once after the nodes have been specified in
        set_test_params()."""
        if self.bind_to_localhost_only:
            extra_confs = [["bind=127.0.0.1"]] * num_nodes
        else:
            extra_confs = [[]] * num_nodes
        if extra_args is None:
            extra_args = [[]] * num_nodes
        if binary is None:
            binary = [self.options.bitcoind] * num_nodes
        assert_equal(len(extra_confs), num_nodes)
        assert_equal(len(extra_args), num_nodes)
        assert_equal(len(binary), num_nodes)
        old_num_nodes = len(self.nodes)
        for i in range(num_nodes):
            self.nodes.append(TestNode(
                old_num_nodes + i,
                get_datadir_path(self.options.tmpdir, old_num_nodes + i),
                self.extra_args_from_options,
                chain=self.chain,
                rpchost=rpchost,
                timewait=self.rpc_timeout,
                bitcoind=binary[i],
                bitcoin_cli=self.options.bitcoincli,
                mocktime=self.mocktime,
                coverage_dir=self.options.coveragedir,
                cwd=self.options.tmpdir,
                extra_conf=extra_confs[i],
                extra_args=extra_args[i],
                use_cli=self.options.usecli,
                start_perf=self.options.perf,
                use_valgrind=self.options.valgrind,
            ))

    def start_node(self, i, *args, **kwargs):
        """Start a dashd"""

        node = self.nodes[i]

        node.start(*args, **kwargs)
        node.wait_for_rpc_connection()

        if self.options.coveragedir is not None:
            coverage.write_all_rpc_commands(self.options.coveragedir, node.rpc)

    def start_nodes(self, extra_args=None, *args, **kwargs):
        """Start multiple dashds"""

        if extra_args is None:
            extra_args = [None] * self.num_nodes
        assert_equal(len(extra_args), self.num_nodes)
        try:
            for i, node in enumerate(self.nodes):
                node.start(extra_args[i], *args, **kwargs)
            for node in self.nodes:
                node.wait_for_rpc_connection()
        except:
            # If one node failed to start, stop the others
            self.stop_nodes()
            raise

        if self.options.coveragedir is not None:
            for node in self.nodes:
                coverage.write_all_rpc_commands(self.options.coveragedir, node.rpc)

    def stop_node(self, i, expected_stderr='', wait=0):
        """Stop a dashd test node"""
        self.nodes[i].stop_node(expected_stderr=expected_stderr, wait=wait)
        self.nodes[i].wait_until_stopped()

    def stop_nodes(self, expected_stderr='', wait=0):
        """Stop multiple dashd test nodes"""
        for node in self.nodes:
            # Issue RPC to stop nodes
            node.stop_node(expected_stderr=expected_stderr, wait=wait)

        for node in self.nodes:
            # Wait for nodes to stop
            node.wait_until_stopped()

    def restart_node(self, i, extra_args=None, expected_stderr=''):
        """Stop and start a test node"""
        self.stop_node(i, expected_stderr)
        self.start_node(i, extra_args)

    def wait_for_node_exit(self, i, timeout):
        self.nodes[i].process.wait(timeout)

    def split_network(self):
        """
        Split the network of four nodes into nodes 0/1 and 2/3.
        """
        disconnect_nodes(self.nodes[1], 2)
        disconnect_nodes(self.nodes[2], 1)
        self.sync_all(self.nodes[:2])
        self.sync_all(self.nodes[2:])

    def join_network(self):
        """
        Join the (previously split) network halves together.
        """
        connect_nodes(self.nodes[1], 2)
        self.sync_all()

    def sync_blocks(self, nodes=None, **kwargs):
        sync_blocks(nodes or self.nodes, **kwargs)

    def sync_mempools(self, nodes=None, **kwargs):
        if self.mocktime != 0:
            if 'wait' not in kwargs:
                kwargs['wait'] = 0.1
            if 'wait_func' not in kwargs:
                kwargs['wait_func'] = lambda: self.bump_mocktime(3, nodes=nodes)

        sync_mempools(nodes or self.nodes, **kwargs)

    def sync_all(self, nodes=None, **kwargs):
        self.sync_blocks(nodes, **kwargs)
        self.sync_mempools(nodes, **kwargs)

    def disable_mocktime(self):
        self.mocktime = 0
        for node in self.nodes:
            node.mocktime = 0

    def bump_mocktime(self, t, update_nodes=True, nodes=None):
        self.mocktime += t
        if update_nodes:
            set_node_times(nodes or self.nodes, self.mocktime)

    def set_cache_mocktime(self):
        # For backwared compatibility of the python scripts
        # with previous versions of the cache, set MOCKTIME
        # to regtest genesis time + (201 * 156)
        self.mocktime = TIME_GENESIS_BLOCK + (201 * 156)
        for node in self.nodes:
            node.mocktime = self.mocktime

    def set_genesis_mocktime(self):
        self.mocktime = TIME_GENESIS_BLOCK
        for node in self.nodes:
            node.mocktime = self.mocktime

    # Private helper methods. These should not be accessed by the subclass test scripts.

    def _start_logging(self):
        # Add logger and logging handlers
        self.log = logging.getLogger('TestFramework')
        self.log.setLevel(logging.DEBUG)
        # Create file handler to log all messages
        fh = logging.FileHandler(self.options.tmpdir + '/test_framework.log', encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        # Create console handler to log messages to stderr. By default this logs only error messages, but can be configured with --loglevel.
        ch = logging.StreamHandler(sys.stdout)
        # User can provide log level as a number or string (eg DEBUG). loglevel was caught as a string, so try to convert it to an int
        ll = int(self.options.loglevel) if self.options.loglevel.isdigit() else self.options.loglevel.upper()
        ch.setLevel(ll)
        # Format logs the same as dashd's debug.log with microprecision (so log files can be concatenated and sorted)
        formatter = logging.Formatter(fmt='%(asctime)s.%(msecs)03d000Z %(name)s (%(levelname)s): %(message)s', datefmt='%Y-%m-%dT%H:%M:%S')
        formatter.converter = time.gmtime
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        # add the handlers to the logger
        self.log.addHandler(fh)
        self.log.addHandler(ch)

        if self.options.trace_rpc:
            rpc_logger = logging.getLogger("BitcoinRPC")
            rpc_logger.setLevel(logging.DEBUG)
            rpc_handler = logging.StreamHandler(sys.stdout)
            rpc_handler.setLevel(logging.DEBUG)
            rpc_logger.addHandler(rpc_handler)

    def _initialize_chain(self, extra_args=None):
        """Initialize a pre-mined blockchain for use by the test.

        Create a cache of a 200-block-long chain (with wallet) for MAX_NODES
        Afterward, create num_nodes copies from the cache."""

        assert self.num_nodes <= MAX_NODES
        create_cache = False
        for i in range(MAX_NODES):
            if not os.path.isdir(get_datadir_path(self.options.cachedir, i)):
                create_cache = True
                break

        if create_cache:
            self.log.debug("Creating data directories from cached datadir")

            # find and delete old cache directories if any exist
            for i in range(MAX_NODES):
                if os.path.isdir(get_datadir_path(self.options.cachedir, i)):
                    shutil.rmtree(get_datadir_path(self.options.cachedir, i))

            # Create cache directories, run dashds:
            self.set_genesis_mocktime()
            for i in range(MAX_NODES):
                datadir = initialize_datadir(self.options.cachedir, i, self.chain)
                args = [self.options.bitcoind, "-datadir=" + datadir, "-mocktime="+str(TIME_GENESIS_BLOCK), '-disablewallet']
                if i > 0:
                    args.append("-connect=127.0.0.1:" + str(p2p_port(0)))
                if extra_args is not None:
                    args.extend(extra_args)
                self.nodes.append(TestNode(i, get_datadir_path(self.options.cachedir, i), chain=self.chain, extra_conf=["bind=127.0.0.1"], extra_args=[], extra_args_from_options=self.extra_args_from_options, rpchost=None,
                    timewait=self.rpc_timeout,
                    bitcoind=self.options.bitcoind,
                    bitcoin_cli=self.options.bitcoincli,
                    mocktime=self.mocktime,
                    coverage_dir=None,
                    cwd=self.options.tmpdir,
                ))
                self.nodes[i].args = args
                self.start_node(i)

            # Wait for RPC connections to be ready
            for node in self.nodes:
                node.wait_for_rpc_connection()

            # Create a 200-block-long chain; each of the 4 first nodes
            # gets 25 mature blocks and 25 immature.
            # Note: To preserve compatibility with older versions of
            # initialize_chain, only 4 nodes will generate coins.
            #
            # blocks are created with timestamps 10 minutes apart
            # starting from 2010 minutes in the past
            block_time = TIME_GENESIS_BLOCK
            for i in range(2):
                for peer in range(4):
                    for j in range(25):
                        set_node_times(self.nodes, block_time)
                        self.nodes[peer].generatetoaddress(1, self.nodes[peer].get_deterministic_priv_key().address)
                        block_time += 156
                    # Must sync before next peer starts generating blocks
                    self.sync_blocks()

            # Shut them down, and clean up cache directories:
            self.stop_nodes()
            self.nodes = []
            self.disable_mocktime()

            def cache_path(n, *paths):
                chain = get_chain_folder(get_datadir_path(self.options.cachedir, n), self.chain)
                return os.path.join(get_datadir_path(self.options.cachedir, n), chain, *paths)

            for i in range(MAX_NODES):
                os.rmdir(cache_path(i, 'wallets'))  # Remove empty wallets dir
                for entry in os.listdir(cache_path(i)):
                    if entry not in ['chainstate', 'blocks', 'indexes', 'evodb', 'llmq', 'backups']:
                        os.remove(cache_path(i, entry))

        for i in range(self.num_nodes):
            from_dir = get_datadir_path(self.options.cachedir, i)
            to_dir = get_datadir_path(self.options.tmpdir, i)
            shutil.copytree(from_dir, to_dir)
            initialize_datadir(self.options.tmpdir, i, self.chain)  # Overwrite port/rpcport in dash.conf

    def _initialize_chain_clean(self):
        """Initialize empty blockchain for use by the test.

        Create an empty blockchain and num_nodes wallets.
        Useful if a test case wants complete control over initialization."""
        for i in range(self.num_nodes):
            initialize_datadir(self.options.tmpdir, i, self.chain)

    def skip_if_no_py3_zmq(self):
        """Attempt to import the zmq package and skip the test if the import fails."""
        try:
            import zmq  # noqa
        except ImportError:
            raise SkipTest("python3-zmq module not available.")

    def skip_if_no_bitcoind_zmq(self):
        """Skip the running test if dashd has not been compiled with zmq support."""
        if not self.is_zmq_compiled():
            raise SkipTest("dashd has not been built with zmq enabled.")

    def skip_if_no_wallet(self):
        """Skip the running test if wallet has not been compiled."""
        if not self.is_wallet_compiled():
            raise SkipTest("wallet has not been compiled.")

    def skip_if_no_wallet_tool(self):
        """Skip the running test if dash-wallet has not been compiled."""
        if not self.is_wallet_tool_compiled():
            raise SkipTest("dash-wallet has not been compiled")

    def skip_if_no_cli(self):
        """Skip the running test if dash-cli has not been compiled."""
        if not self.is_cli_compiled():
            raise SkipTest("dash-cli has not been compiled.")

    def is_cli_compiled(self):
        """Checks whether dash-cli was compiled."""
        return self.config["components"].getboolean("ENABLE_CLI")

    def is_wallet_compiled(self):
        """Checks whether the wallet module was compiled."""
        return self.config["components"].getboolean("ENABLE_WALLET")

    def is_wallet_tool_compiled(self):
        """Checks whether dash-wallet was compiled."""
        return self.config["components"].getboolean("ENABLE_WALLET_TOOL")

    def is_zmq_compiled(self):
        """Checks whether the zmq module was compiled."""
        return self.config["components"].getboolean("ENABLE_ZMQ")


MASTERNODE_COLLATERAL = 1000


class MasternodeInfo:
    def __init__(self, proTxHash, ownerAddr, votingAddr, pubKeyOperator, keyOperator, collateral_address, collateral_txid, collateral_vout):
        self.proTxHash = proTxHash
        self.ownerAddr = ownerAddr
        self.votingAddr = votingAddr
        self.pubKeyOperator = pubKeyOperator
        self.keyOperator = keyOperator
        self.collateral_address = collateral_address
        self.collateral_txid = collateral_txid
        self.collateral_vout = collateral_vout


class DashTestFramework(BitcoinTestFramework):
    def set_test_params(self):
        """Tests must this method to change default values for number of nodes, topology, etc"""
        raise NotImplementedError

    def skip_test_if_missing_module(self):
        self.skip_if_no_wallet()

    def run_test(self):
        """Tests must override this method to define test logic"""
        raise NotImplementedError

    def set_dash_test_params(self, num_nodes, masterodes_count, extra_args=None, fast_dip3_enforcement=False):
        self.mn_count = masterodes_count
        self.num_nodes = num_nodes
        self.mninfo = []
        self.setup_clean_chain = True
        # additional args
        if extra_args is None:
            extra_args = [[]] * num_nodes
        assert_equal(len(extra_args), num_nodes)
        self.extra_args = [copy.deepcopy(a) for a in extra_args]
        self.extra_args[0] += ["-sporkkey=cP4EKFyJsHT39LDqgdcB43Y3YXjNyjb5Fuas1GQSeAtjnZWmZEQK"]
        self.fast_dip3_enforcement = fast_dip3_enforcement
        if fast_dip3_enforcement:
            for i in range(0, num_nodes):
                self.extra_args[i].append("-dip3params=30:50")

        # make sure to activate dip8 after prepare_masternodes has finished its job already
        self.set_dash_dip8_activation(200)

        # LLMQ default test params (no need to pass -llmqtestparams)
        self.llmq_size = 3
        self.llmq_threshold = 2

        # This is nRequestTimeout in dash-q-recovery thread
        self.quorum_data_thread_request_timeout_seconds = 10
        # This is EXPIRATION_TIMEOUT in CQuorumDataRequest
        self.quorum_data_request_expiration_timeout = 300

    def set_dash_dip8_activation(self, activate_after_block):
        self.dip8_activation_height = activate_after_block
        for i in range(0, self.num_nodes):
            self.extra_args[i].append("-dip8params=%d" % (activate_after_block))

    def activate_dip8(self, slow_mode=False):
        # NOTE: set slow_mode=True if you are activating dip8 after a huge reorg
        # or nodes might fail to catch up otherwise due to a large
        # (MAX_BLOCKS_IN_TRANSIT_PER_PEER = 16 blocks) reorg error.
        self.log.info("Wait for dip0008 activation")
        while self.nodes[0].getblockcount() < self.dip8_activation_height:
            self.nodes[0].generate(10)
            if slow_mode:
                self.sync_blocks()
        self.sync_blocks()

    def activate_dip0024(self, slow_mode=False, expected_activation_height=None):
        self.log.info("Wait for dip0024 activation")

        if expected_activation_height is not None:
            height = self.nodes[0].getblockcount()
            batch_size = 100
            while height - expected_activation_height > batch_size:
                self.nodes[0].generate(batch_size)
                height += batch_size
                self.sync_blocks()
            assert height - expected_activation_height < batch_size
            self.nodes[0].generate(height - expected_activation_height - 1)
            self.sync_blocks()
            assert self.nodes[0].getblockchaininfo()['bip9_softforks']['dip0024']['status'] != 'active'

        while self.nodes[0].getblockchaininfo()['bip9_softforks']['dip0024']['status'] != 'active':
            self.nodes[0].generate(10)
            if slow_mode:
                self.sync_blocks()
        self.sync_blocks()

    def set_dash_llmq_test_params(self, llmq_size, llmq_threshold):
        self.llmq_size = llmq_size
        self.llmq_threshold = llmq_threshold
        for i in range(0, self.num_nodes):
            self.extra_args[i].append("-llmqtestparams=%d:%d" % (self.llmq_size, self.llmq_threshold))
            self.extra_args[i].append("-llmqtestinstantsendparams=%d:%d" % (self.llmq_size, self.llmq_threshold))

    def create_simple_node(self):
        idx = len(self.nodes)
        self.add_nodes(1, extra_args=[self.extra_args[idx]])
        self.start_node(idx)
        for i in range(0, idx):
            connect_nodes(self.nodes[i], idx)

    def prepare_masternodes(self):
        self.log.info("Preparing %d masternodes" % self.mn_count)
        rewardsAddr = self.nodes[0].getnewaddress()

        for idx in range(0, self.mn_count):
            self.prepare_masternode(idx, rewardsAddr)
        self.sync_all()

    def prepare_masternode(self, idx, rewardsAddr=None):

        register_fund = (idx % 2) == 0

        bls = self.nodes[0].bls('generate')
        address = self.nodes[0].getnewaddress()

        txid = None
        txid = self.nodes[0].sendtoaddress(address, MASTERNODE_COLLATERAL)
        collateral_vout = 0
        if not register_fund:
            txraw = self.nodes[0].getrawtransaction(txid, True)
            for vout_idx in range(0, len(txraw["vout"])):
                vout = txraw["vout"][vout_idx]
                if vout["value"] == MASTERNODE_COLLATERAL:
                    collateral_vout = vout_idx
            self.nodes[0].lockunspent(False, [{'txid': txid, 'vout': collateral_vout}])

        # send to same address to reserve some funds for fees
        self.nodes[0].sendtoaddress(address, 0.001)

        ownerAddr = self.nodes[0].getnewaddress()
        # votingAddr = self.nodes[0].getnewaddress()
        if rewardsAddr is None:
            rewardsAddr = self.nodes[0].getnewaddress()
        votingAddr = ownerAddr
        # rewardsAddr = ownerAddr

        port = p2p_port(len(self.nodes) + idx)
        ipAndPort = '127.0.0.1:%d' % port
        operatorReward = idx

        submit = (idx % 4) < 2

        if register_fund:
            # self.nodes[0].lockunspent(True, [{'txid': txid, 'vout': collateral_vout}])
            protx_result = self.nodes[0].protx('register_fund', address, ipAndPort, ownerAddr, bls['public'], votingAddr, operatorReward, rewardsAddr, address, submit)
        else:
            self.nodes[0].generate(1)
            protx_result = self.nodes[0].protx('register', txid, collateral_vout, ipAndPort, ownerAddr, bls['public'], votingAddr, operatorReward, rewardsAddr, address, submit)

        if submit:
            proTxHash = protx_result
        else:
            proTxHash = self.nodes[0].sendrawtransaction(protx_result)


        if operatorReward > 0:
            self.nodes[0].generate(1)
            operatorPayoutAddress = self.nodes[0].getnewaddress()
            self.nodes[0].protx('update_service', proTxHash, ipAndPort, bls['secret'], operatorPayoutAddress, address)

        self.mninfo.append(MasternodeInfo(proTxHash, ownerAddr, votingAddr, bls['public'], bls['secret'], address, txid, collateral_vout))
        # self.sync_all()

        self.log.info("Prepared masternode %d: collateral_txid=%s, collateral_vout=%d, protxHash=%s" % (idx, txid, collateral_vout, proTxHash))

    def remove_masternode(self, idx):
        mn = self.mninfo[idx]
        rawtx = self.nodes[0].createrawtransaction([{"txid": mn.collateral_txid, "vout": mn.collateral_vout}], {self.nodes[0].getnewaddress(): 999.9999})
        rawtx = self.nodes[0].signrawtransactionwithwallet(rawtx)
        self.nodes[0].sendrawtransaction(rawtx["hex"])
        self.nodes[0].generate(1)
        self.sync_all()
        self.mninfo.remove(mn)

        self.log.info("Removed masternode %d", idx)

    def prepare_datadirs(self):
        # stop faucet node so that we can copy the datadir
        self.stop_node(0)

        start_idx = len(self.nodes)
        for idx in range(0, self.mn_count):
            copy_datadir(0, idx + start_idx, self.options.tmpdir, self.chain)

        # restart faucet node
        self.start_node(0)
        force_finish_mnsync(self.nodes[0])

    def start_masternodes(self):
        self.log.info("Starting %d masternodes", self.mn_count)

        start_idx = len(self.nodes)

        self.add_nodes(self.mn_count)
        executor = ThreadPoolExecutor(max_workers=20)

        def do_connect(idx):
            # Connect to the control node only, masternodes should take care of intra-quorum connections themselves
            connect_nodes(self.mninfo[idx].node, 0)

        jobs = []

        # start up nodes in parallel
        for idx in range(0, self.mn_count):
            self.mninfo[idx].nodeIdx = idx + start_idx
            jobs.append(executor.submit(self.start_masternode, self.mninfo[idx]))

        # wait for all nodes to start up
        for job in jobs:
            job.result()
        jobs.clear()

        # connect nodes in parallel
        for idx in range(0, self.mn_count):
            jobs.append(executor.submit(do_connect, idx))

        # wait for all nodes to connect
        for job in jobs:
            job.result()
        jobs.clear()

        executor.shutdown()

    def start_masternode(self, mninfo, extra_args=None):
        args = ['-masternodeblsprivkey=%s' % mninfo.keyOperator] + self.extra_args[mninfo.nodeIdx]
        if extra_args is not None:
            args += extra_args
        self.start_node(mninfo.nodeIdx, extra_args=args)
        mninfo.node = self.nodes[mninfo.nodeIdx]
        force_finish_mnsync(mninfo.node)

    def setup_network(self):
        self.log.info("Creating and starting controller node")
        self.add_nodes(1, extra_args=[self.extra_args[0]])
        self.start_node(0)
        required_balance = MASTERNODE_COLLATERAL * self.mn_count + 1
        self.log.info("Generating %d coins" % required_balance)
        while self.nodes[0].getbalance() < required_balance:
            self.bump_mocktime(1)
            self.nodes[0].generate(10)
        num_simple_nodes = self.num_nodes - self.mn_count - 1
        self.log.info("Creating and starting %s simple nodes", num_simple_nodes)
        for i in range(0, num_simple_nodes):
            self.create_simple_node()

        self.log.info("Activating DIP3")
        if not self.fast_dip3_enforcement:
            while self.nodes[0].getblockcount() < 500:
                self.nodes[0].generate(10)
        self.sync_all()

        # create masternodes
        self.prepare_masternodes()
        self.prepare_datadirs()
        self.start_masternodes()

        # non-masternodes where disconnected from the control node during prepare_datadirs,
        # let's reconnect them back to make sure they receive updates
        for i in range(0, num_simple_nodes):
            connect_nodes(self.nodes[i+1], 0)

        self.bump_mocktime(1)
        self.nodes[0].generate(1)
        # sync nodes
        self.sync_all()
        for i in range(0, num_simple_nodes):
            force_finish_mnsync(self.nodes[i + 1])

        # Enable InstantSend (including block filtering) and ChainLocks by default
        self.nodes[0].spork("SPORK_2_INSTANTSEND_ENABLED", 0)
        self.nodes[0].spork("SPORK_3_INSTANTSEND_BLOCK_FILTERING", 0)
        self.nodes[0].spork("SPORK_19_CHAINLOCKS_ENABLED", 0)
        self.wait_for_sporks_same()
        self.bump_mocktime(1)

        mn_info = self.nodes[0].masternodelist("status")
        assert len(mn_info) == self.mn_count
        for status in mn_info.values():
            assert status == 'ENABLED'

    def create_raw_tx(self, node_from, node_to, amount, min_inputs, max_inputs):
        assert min_inputs <= max_inputs
        # fill inputs
        inputs = []
        balances = node_from.listunspent()
        in_amount = 0.0
        last_amount = 0.0
        for tx in balances:
            if len(inputs) < min_inputs:
                input = {}
                input["txid"] = tx['txid']
                input['vout'] = tx['vout']
                in_amount += float(tx['amount'])
                inputs.append(input)
            elif in_amount > amount:
                break
            elif len(inputs) < max_inputs:
                input = {}
                input["txid"] = tx['txid']
                input['vout'] = tx['vout']
                in_amount += float(tx['amount'])
                inputs.append(input)
            else:
                input = {}
                input["txid"] = tx['txid']
                input['vout'] = tx['vout']
                in_amount -= last_amount
                in_amount += float(tx['amount'])
                inputs[-1] = input
            last_amount = float(tx['amount'])

        assert len(inputs) >= min_inputs
        assert len(inputs) <= max_inputs
        assert in_amount >= amount
        # fill outputs
        receiver_address = node_to.getnewaddress()
        change_address = node_from.getnewaddress()
        fee = 0.001
        outputs = {}
        outputs[receiver_address] = satoshi_round(amount)
        outputs[change_address] = satoshi_round(in_amount - amount - fee)
        rawtx = node_from.createrawtransaction(inputs, outputs)
        ret = node_from.signrawtransactionwithwallet(rawtx)
        decoded = node_from.decoderawtransaction(ret['hex'])
        ret = {**decoded, **ret}
        return ret

    def wait_for_tx(self, txid, node, expected=True, timeout=15):
        def check_tx():
            try:
                return node.getrawtransaction(txid)
            except:
                return False
        if wait_until(check_tx, timeout=timeout, sleep=0.5, do_assert=expected) and not expected:
            raise AssertionError("waiting unexpectedly succeeded")

    def create_islock(self, hextx, deterministic=False):
        tx = FromHex(CTransaction(), hextx)
        tx.rehash()

        request_id_buf = ser_string(b"islock") + ser_compact_size(len(tx.vin))
        inputs = []
        for txin in tx.vin:
            request_id_buf += txin.prevout.serialize()
            inputs.append(txin.prevout)
        request_id = hash256(request_id_buf)[::-1].hex()
        message_hash = tx.hash

        llmq_type = 103 if deterministic else 104
        quorum_member = None
        for mn in self.mninfo:
            res = mn.node.quorum('sign', llmq_type, request_id, message_hash)
            if (res and quorum_member is None):
                quorum_member = mn

        rec_sig = self.get_recovered_sig(request_id, message_hash, node=quorum_member.node, llmq_type=llmq_type)

        if deterministic:
            block_count = quorum_member.node.getblockcount()
            cycle_hash = int(quorum_member.node.getblockhash(block_count - (block_count % 24)), 16)
            islock = msg_isdlock(1, inputs, tx.sha256, cycle_hash, hex_str_to_bytes(rec_sig['sig']))
        else:
            islock = msg_islock(inputs, tx.sha256, hex_str_to_bytes(rec_sig['sig']))

        return islock

    def wait_for_instantlock(self, txid, node, expected=True, timeout=15):
        def check_instantlock():
            try:
                return node.getrawtransaction(txid, True)["instantlock"]
            except:
                return False
        if wait_until(check_instantlock, timeout=timeout, sleep=0.5, do_assert=expected) and not expected:
            raise AssertionError("waiting unexpectedly succeeded")

    def wait_for_chainlocked_block(self, node, block_hash, expected=True, timeout=15):
        def check_chainlocked_block():
            try:
                block = node.getblock(block_hash)
                return block["confirmations"] > 0 and block["chainlock"]
            except:
                return False
        if wait_until(check_chainlocked_block, timeout=timeout, sleep=0.1, do_assert=expected) and not expected:
            raise AssertionError("waiting unexpectedly succeeded")

    def wait_for_chainlocked_block_all_nodes(self, block_hash, timeout=15):
        for node in self.nodes:
            self.wait_for_chainlocked_block(node, block_hash, timeout=timeout)

    def wait_for_best_chainlock(self, node, block_hash, timeout=15):
        wait_until(lambda: node.getbestchainlock()["blockhash"] == block_hash, timeout=timeout, sleep=0.1)

    def wait_for_sporks_same(self, timeout=30):
        def check_sporks_same():
            sporks = self.nodes[0].spork('show')
            return all(node.spork('show') == sporks for node in self.nodes[1:])
        wait_until(check_sporks_same, timeout=timeout, sleep=0.5)

    def wait_for_quorum_connections(self, quorum_hash, expected_connections, nodes, llmq_type_name="llmq_test", timeout = 60, wait_proc=None):
        def check_quorum_connections():
            all_ok = True
            for node in nodes:
                s = node.quorum("dkgstatus")
                mn_ok = True
                for qs in s:
                    if "llmqType" not in qs:
                        continue
                    if qs["llmqType"] != llmq_type_name:
                        continue
                    if "quorumConnections" not in qs:
                        continue
                    qconnections = qs["quorumConnections"]
                    if qconnections["quorumHash"] != quorum_hash:
                        mn_ok = False
                        continue
                    cnt = 0
                    for c in qconnections["quorumConnections"]:
                        if c["connected"]:
                            cnt += 1
                    if cnt < expected_connections:
                        mn_ok = False
                        break
                    break
                if not mn_ok:
                    all_ok = False
                    break
            if not all_ok and wait_proc is not None:
                wait_proc()
            return all_ok
        wait_until(check_quorum_connections, timeout=timeout, sleep=1)

    def wait_for_masternode_probes(self, mninfos, timeout = 30, wait_proc=None, llmq_type_name="llmq_test"):
        def check_probes():
            def ret():
                if wait_proc is not None:
                    wait_proc()
                return False

            for mn in mninfos:
                s = mn.node.quorum('dkgstatus')
                if llmq_type_name not in s["session"]:
                    continue
                if "quorumConnections" not in s:
                    return ret()
                s = s["quorumConnections"]
                if llmq_type_name not in s:
                    return ret()

                for c in s[llmq_type_name]:
                    if c["proTxHash"] == mn.proTxHash:
                        continue
                    if not c["outbound"]:
                        mn2 = mn.node.protx('info', c["proTxHash"])
                        if [m for m in mninfos if c["proTxHash"] == m.proTxHash]:
                            # MN is expected to be online and functioning, so let's verify that the last successful
                            # probe is not too old. Probes are retried after 50 minutes, while DKGs consider a probe
                            # as failed after 60 minutes
                            if mn2['metaInfo']['lastOutboundSuccessElapsed'] > 55 * 60:
                                return ret()
                        else:
                            # MN is expected to be offline, so let's only check that the last probe is not too long ago
                            if mn2['metaInfo']['lastOutboundAttemptElapsed'] > 55 * 60 and mn2['metaInfo']['lastOutboundSuccessElapsed'] > 55 * 60:
                                return ret()

            return True
        wait_until(check_probes, timeout=timeout, sleep=1)

    def wait_for_quorum_phase(self, quorum_hash, phase, expected_member_count, check_received_messages, check_received_messages_count, mninfos, llmq_type_name="llmq_test", timeout=30, sleep=1):
        def check_dkg_session():
            all_ok = True
            member_count = 0
            for mn in mninfos:
                s = mn.node.quorum("dkgstatus")["session"]
                mn_ok = True
                for qs in s:
                    if qs["llmqType"] != llmq_type_name:
                        continue
                    qstatus = qs["status"]
                    if qstatus["quorumHash"] != quorum_hash:
                        continue
                    member_count += 1
                    if "phase" not in qstatus:
                        mn_ok = False
                        break
                    if qstatus["phase"] != phase:
                        mn_ok = False
                        break
                    if check_received_messages is not None:
                        if qstatus[check_received_messages] < check_received_messages_count:
                            mn_ok = False
                            break
                    break
                if not mn_ok:
                    all_ok = False
                    break
            if all_ok and member_count != expected_member_count:
                return False
            return all_ok
        wait_until(check_dkg_session, timeout=timeout, sleep=sleep)

    def wait_for_quorum_commitment(self, quorum_hash, nodes, llmq_type=100, timeout=15):
        def check_dkg_comitments():
            time.sleep(2)
            all_ok = True
            for node in nodes:
                s = node.quorum("dkgstatus")
                if "minableCommitments" not in s:
                    all_ok = False
                    break
                commits = s["minableCommitments"]
                c_ok = False
                for c in commits:
                    if c["llmqType"] != llmq_type:
                        continue
                    if c["quorumHash"] != quorum_hash:
                        continue
                    c_ok = True
                    break
                if not c_ok:
                    all_ok = False
                    break
            return all_ok
        wait_until(check_dkg_comitments, timeout=timeout, sleep=1)

    def wait_for_quorum_list(self, quorum_hash, nodes, timeout=15, sleep=2, llmq_type_name="llmq_test"):
        def wait_func():
            self.log.info("quorums: " + str(self.nodes[0].quorum("list")))
            if quorum_hash in self.nodes[0].quorum("list")[llmq_type_name]:
                return True
            self.bump_mocktime(sleep, nodes=nodes)
            self.nodes[0].generate(1)
            sync_blocks(nodes)
            return False
        wait_until(wait_func, timeout=timeout, sleep=sleep)

    def wait_for_quorums_list(self, quorum_hash_0, quorum_hash_1, nodes, llmq_type_name="llmq_test",  timeout=15, sleep=2):
        def wait_func():
            self.log.info("h("+str(self.nodes[0].getblockcount())+") quorums: " + str(self.nodes[0].quorum("list")))
            if quorum_hash_0 in self.nodes[0].quorum("list")[llmq_type_name]:
                if quorum_hash_1 in self.nodes[0].quorum("list")[llmq_type_name]:
                    return True
            self.bump_mocktime(sleep, nodes=nodes)
            self.nodes[0].generate(1)
            sync_blocks(nodes)
            return False
        wait_until(wait_func, timeout=timeout, sleep=sleep)

    def move_blocks(self, nodes, num_blocks):
        time.sleep(1)
        self.bump_mocktime(1, nodes=nodes)
        self.nodes[0].generate(num_blocks)
        sync_blocks(nodes)

    def mine_quorum(self, llmq_type_name="llmq_test", llmq_type=100, expected_connections=None, expected_members=None, expected_contributions=None, expected_complaints=0, expected_justifications=0, expected_commitments=None, mninfos_online=None, mninfos_valid=None):
        spork21_active = self.nodes[0].spork('show')['SPORK_21_QUORUM_ALL_CONNECTED'] <= 1
        spork23_active = self.nodes[0].spork('show')['SPORK_23_QUORUM_POSE'] <= 1

        if expected_connections is None:
            expected_connections = (self.llmq_size - 1) if spork21_active else 2
        if expected_members is None:
            expected_members = self.llmq_size
        if expected_contributions is None:
            expected_contributions = self.llmq_size
        if expected_commitments is None:
            expected_commitments = self.llmq_size
        if mninfos_online is None:
            mninfos_online = self.mninfo.copy()
        if mninfos_valid is None:
            mninfos_valid = self.mninfo.copy()

        self.log.info("Mining quorum: llmq_type_name=%s, llmq_type=%d, expected_members=%d, expected_connections=%d, expected_contributions=%d, expected_complaints=%d, expected_justifications=%d, "
                      "expected_commitments=%d" % (llmq_type_name, llmq_type, expected_members, expected_connections, expected_contributions, expected_complaints,
                                                   expected_justifications, expected_commitments))

        nodes = [self.nodes[0]] + [mn.node for mn in mninfos_online]

        # move forward to next DKG
        skip_count = 24 - (self.nodes[0].getblockcount() % 24)
        if skip_count != 0:
            self.bump_mocktime(1, nodes=nodes)
            self.nodes[0].generate(skip_count)
        sync_blocks(nodes)

        q = self.nodes[0].getbestblockhash()
        self.log.info("Expected quorum_hash:"+str(q))
        self.log.info("Waiting for phase 1 (init)")
        self.wait_for_quorum_phase(q, 1, expected_members, None, 0, mninfos_online, llmq_type_name=llmq_type_name)
        self.wait_for_quorum_connections(q, expected_connections, nodes, wait_proc=lambda: self.bump_mocktime(1, nodes=nodes), llmq_type_name=llmq_type_name)
        if spork23_active:
            self.wait_for_masternode_probes(mninfos_valid, wait_proc=lambda: self.bump_mocktime(1, nodes=nodes))

        self.move_blocks(nodes, 2)

        self.log.info("Waiting for phase 2 (contribute)")
        self.wait_for_quorum_phase(q, 2, expected_members, "receivedContributions", expected_contributions, mninfos_online, llmq_type_name=llmq_type_name)

        self.move_blocks(nodes, 2)

        self.log.info("Waiting for phase 3 (complain)")
        self.wait_for_quorum_phase(q, 3, expected_members, "receivedComplaints", expected_complaints, mninfos_online, llmq_type_name=llmq_type_name)

        self.move_blocks(nodes, 2)

        self.log.info("Waiting for phase 4 (justify)")
        self.wait_for_quorum_phase(q, 4, expected_members, "receivedJustifications", expected_justifications, mninfos_online, llmq_type_name=llmq_type_name)

        self.move_blocks(nodes, 2)

        self.log.info("Waiting for phase 5 (commit)")
        self.wait_for_quorum_phase(q, 5, expected_members, "receivedPrematureCommitments", expected_commitments, mninfos_online, llmq_type_name=llmq_type_name)

        self.move_blocks(nodes, 2)

        self.log.info("Waiting for phase 6 (mining)")
        self.wait_for_quorum_phase(q, 6, expected_members, None, 0, mninfos_online, llmq_type_name=llmq_type_name)

        self.log.info("Waiting final commitment")
        self.wait_for_quorum_commitment(q, nodes, llmq_type=llmq_type)

        self.log.info("Mining final commitment")
        self.bump_mocktime(1, nodes=nodes)
        self.nodes[0].getblocktemplate() # this calls CreateNewBlock
        self.nodes[0].generate(1)
        sync_blocks(nodes)

        self.log.info("Waiting for quorum to appear in the list")
        self.wait_for_quorum_list(q, nodes, llmq_type_name=llmq_type_name)

        new_quorum = self.nodes[0].quorum("list", 1)[llmq_type_name][0]
        assert_equal(q, new_quorum)
        quorum_info = self.nodes[0].quorum("info", llmq_type, new_quorum)

        # Mine 8 (SIGN_HEIGHT_OFFSET) more blocks to make sure that the new quorum gets eligible for signing sessions
        self.nodes[0].generate(8)

        sync_blocks(nodes)

        self.log.info("New quorum: height=%d, quorumHash=%s, quorumIndex=%d, minedBlock=%s" % (quorum_info["height"], new_quorum, quorum_info["quorumIndex"], quorum_info["minedBlock"]))

        return new_quorum

    def mine_cycle_quorum(self, llmq_type_name="llmq_test_dip0024", llmq_type=103,  expected_connections=None, expected_members=None, expected_contributions=None, expected_complaints=0, expected_justifications=0, expected_commitments=None, mninfos_online=None, mninfos_valid=None):
        spork21_active = self.nodes[0].spork('show')['SPORK_21_QUORUM_ALL_CONNECTED'] <= 1
        spork23_active = self.nodes[0].spork('show')['SPORK_23_QUORUM_POSE'] <= 1

        if expected_connections is None:
            expected_connections = (self.llmq_size - 1) if spork21_active else 2
        if expected_members is None:
            expected_members = self.llmq_size
        if expected_contributions is None:
            expected_contributions = self.llmq_size
        if expected_commitments is None:
            expected_commitments = self.llmq_size
        if mninfos_online is None:
            mninfos_online = self.mninfo.copy()
        if mninfos_valid is None:
            mninfos_valid = self.mninfo.copy()

        self.log.info("Mining quorum: expected_members=%d, expected_connections=%d, expected_contributions=%d, expected_complaints=%d, expected_justifications=%d, "
                      "expected_commitments=%d" % (expected_members, expected_connections, expected_contributions, expected_complaints,
                                                   expected_justifications, expected_commitments))

        nodes = [self.nodes[0]] + [mn.node for mn in mninfos_online]

        # move forward to next DKG
        skip_count = 24 - (self.nodes[0].getblockcount() % 24)

        # if skip_count != 0:
        #     self.bump_mocktime(1, nodes=nodes)
        #     self.nodes[0].generate(skip_count)
        #     time.sleep(4)
        # sync_blocks(nodes)

        self.move_blocks(nodes, skip_count)

        q_0 = self.nodes[0].getbestblockhash()
        self.log.info("Expected quorum_0 at:" + str(self.nodes[0].getblockcount()))
        # time.sleep(4)
        self.log.info("Expected quorum_0 hash:" + str(q_0))
        # time.sleep(4)
        self.log.info("quorumIndex 0: Waiting for phase 1 (init)")
        self.wait_for_quorum_phase(q_0, 1, expected_members, None, 0, mninfos_online, llmq_type_name)
        self.log.info("quorumIndex 0: Waiting for quorum connections (init)")
        self.wait_for_quorum_connections(q_0, expected_connections, nodes, llmq_type_name, wait_proc=lambda: self.bump_mocktime(1, nodes=nodes))
        if spork23_active:
            self.wait_for_masternode_probes(mninfos_valid, wait_proc=lambda: self.bump_mocktime(1, nodes=nodes))

        self.move_blocks(nodes, 1)

        q_1 = self.nodes[0].getbestblockhash()
        self.log.info("Expected quorum_1 at:" + str(self.nodes[0].getblockcount()))
        # time.sleep(2)
        self.log.info("Expected quorum_1 hash:" + str(q_1))
        # time.sleep(2)
        self.log.info("quorumIndex 1: Waiting for phase 1 (init)")
        self.wait_for_quorum_phase(q_1, 1, expected_members, None, 0, mninfos_online, llmq_type_name)
        self.log.info("quorumIndex 1: Waiting for quorum connections (init)")
        self.wait_for_quorum_connections(q_1, expected_connections, nodes, llmq_type_name, wait_proc=lambda: self.bump_mocktime(1, nodes=nodes))

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 0: Waiting for phase 2 (contribute)")
        self.wait_for_quorum_phase(q_0, 2, expected_members, "receivedContributions", expected_contributions, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 1: Waiting for phase 2 (contribute)")
        self.wait_for_quorum_phase(q_1, 2, expected_members, "receivedContributions", expected_contributions, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 0: Waiting for phase 3 (complain)")
        self.wait_for_quorum_phase(q_0, 3, expected_members, "receivedComplaints", expected_complaints, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 1: Waiting for phase 3 (complain)")
        self.wait_for_quorum_phase(q_1, 3, expected_members, "receivedComplaints", expected_complaints, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 0: Waiting for phase 4 (justify)")
        self.wait_for_quorum_phase(q_0, 4, expected_members, "receivedJustifications", expected_justifications, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 1: Waiting for phase 4 (justify)")
        self.wait_for_quorum_phase(q_1, 4, expected_members, "receivedJustifications", expected_justifications, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 0: Waiting for phase 5 (commit)")
        self.wait_for_quorum_phase(q_0, 5, expected_members, "receivedPrematureCommitments", expected_commitments, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 1: Waiting for phase 5 (commit)")
        self.wait_for_quorum_phase(q_1, 5, expected_members, "receivedPrematureCommitments", expected_commitments, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 0: Waiting for phase 6 (finalization)")
        self.wait_for_quorum_phase(q_0, 6, expected_members, None, 0, mninfos_online, llmq_type_name)

        self.move_blocks(nodes, 1)

        self.log.info("quorumIndex 1: Waiting for phase 6 (finalization)")
        self.wait_for_quorum_phase(q_1, 6, expected_members, None, 0, mninfos_online, llmq_type_name)
        time.sleep(6)
        self.log.info("Mining final commitments")
        self.bump_mocktime(1, nodes=nodes)
        self.nodes[0].getblocktemplate() # this calls CreateNewBlock
        self.nodes[0].generate(1)
        sync_blocks(nodes)

        time.sleep(6)
        self.log.info("Waiting for quorum(s) to appear in the list")
        self.wait_for_quorums_list(q_0, q_1, nodes, llmq_type_name)

        quorum_info_0 = self.nodes[0].quorum("info", llmq_type, q_0)
        quorum_info_1 = self.nodes[0].quorum("info", llmq_type, q_1)
        # Mine 8 (SIGN_HEIGHT_OFFSET) more blocks to make sure that the new quorum gets eligible for signing sessions
        self.nodes[0].generate(8)

        sync_blocks(nodes)
        self.log.info("New quorum: height=%d, quorumHash=%s, quorumIndex=%d, minedBlock=%s" % (quorum_info_0["height"], q_0, quorum_info_0["quorumIndex"], quorum_info_0["minedBlock"]))
        self.log.info("New quorum: height=%d, quorumHash=%s, quorumIndex=%d, minedBlock=%s" % (quorum_info_1["height"], q_1, quorum_info_1["quorumIndex"], quorum_info_1["minedBlock"]))

        self.log.info("quorum_info_0:"+str(quorum_info_0))
        self.log.info("quorum_info_1:"+str(quorum_info_1))

        best_block_hash = self.nodes[0].getbestblockhash()
        block_height = self.nodes[0].getblockcount()
        quorum_rotation_info = self.nodes[0].quorum("rotationinfo", best_block_hash)
        self.log.info("h("+str(block_height)+"):"+str(quorum_rotation_info))

        return (quorum_info_0, quorum_info_1)

    def move_to_next_cycle(self):
        cycle_length = 24
        mninfos_online = self.mninfo.copy()
        nodes = [self.nodes[0]] + [mn.node for mn in mninfos_online]
        cur_block = self.nodes[0].getblockcount()

        # move forward to next DKG
        skip_count = cycle_length - (cur_block % cycle_length)
        if skip_count != 0:
            self.bump_mocktime(1, nodes=nodes)
            self.nodes[0].generate(skip_count)
        sync_blocks(nodes)
        time.sleep(1)
        self.log.info('Moved from block %d to %d' % (cur_block, self.nodes[0].getblockcount()))

    def get_recovered_sig(self, rec_sig_id, rec_sig_msg_hash, llmq_type=100, node=None):
        # Note: recsigs aren't relayed to regular nodes by default,
        # make sure to pick a mn as a node to query for recsigs.
        node = self.mninfo[0].node if node is None else node
        time_start = time.time()
        while time.time() - time_start < 10:
            try:
                return node.quorum('getrecsig', llmq_type, rec_sig_id, rec_sig_msg_hash)
            except JSONRPCException:
                time.sleep(0.1)
        assert False

    def get_quorum_masternodes(self, q, llmq_type=100):
        qi = self.nodes[0].quorum('info', llmq_type, q)
        result = []
        for m in qi['members']:
            result.append(self.get_mninfo(m['proTxHash']))
        return result

    def get_mninfo(self, proTxHash):
        for mn in self.mninfo:
            if mn.proTxHash == proTxHash:
                return mn
        return None

    def test_mn_quorum_data(self, test_mn, quorum_type_in, quorum_hash_in, test_secret=True, expect_secret=True):
        quorum_info = test_mn.node.quorum("info", quorum_type_in, quorum_hash_in, True)
        if test_secret and expect_secret != ("secretKeyShare" in quorum_info):
            return False
        if "members" not in quorum_info or len(quorum_info["members"]) == 0:
            return False
        pubkey_count = 0
        valid_count = 0
        for quorum_member in quorum_info["members"]:
            valid_count += quorum_member["valid"]
            pubkey_count += "pubKeyShare" in quorum_member
        return pubkey_count == valid_count

    def wait_for_quorum_data(self, mns, quorum_type_in, quorum_hash_in, test_secret=True, expect_secret=True,
                             recover=False, timeout=60):
        def test_mns():
            valid = 0
            if recover:
                if self.mocktime % 2:
                    self.bump_mocktime(self.quorum_data_request_expiration_timeout + 1)
                    self.nodes[0].generate(1)
                else:
                    self.bump_mocktime(self.quorum_data_thread_request_timeout_seconds + 1)

            for test_mn in mns:
                valid += self.test_mn_quorum_data(test_mn, quorum_type_in, quorum_hash_in, test_secret, expect_secret)
            self.log.debug("wait_for_quorum_data: %d/%d - quorum_type=%d quorum_hash=%s" %
                           (valid, len(mns), quorum_type_in, quorum_hash_in))
            return valid == len(mns)

        wait_until(test_mns, timeout=timeout, sleep=0.5)

    def wait_for_mnauth(self, node, count, timeout=10):
        def test():
            pi = node.getpeerinfo()
            c = 0
            for p in pi:
                if "verified_proregtx_hash" in p and p["verified_proregtx_hash"] != "":
                    c += 1
            return c >= count
        wait_until(test, timeout=timeout)