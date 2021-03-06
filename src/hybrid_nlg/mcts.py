"""
mcts module implement Monte Carlo Tree Search algorithm for grammar search. 
It defines 4 classes : 
- CounterNode -> node wrapper that maintains statistics on the visited part of the tree 
- EvalBuffer -> buffer system used to score the sentences by batch and not one by one 
- ResssourceDistributor -> handle the division of the computationnal ressource and allow the 
    implementation of various strategies to quickly go deeper into the tree 
- MCTS -> implement the classical MCTS steps : selection, expansion, simulation, backpropagation 
"""

import math
from typing import *

from .lm_scorer import SentenceScore
from .grammar_tree import GrammarNode


class CounterNode:
    """
    CounterNode object is used to wrap a given node and maintain several statistics about it during the MCTS : 
    - the nomber of times the node has been visited
    - the expected reward from this node
    - the top reward that has been obtained from this node
    - the square sum reward obtained so far from this node
    - a reference to the leaf corresponding to this top reward 

    The counter node also keeps in memory a reference to his parent node in order to backpropagate
    the information that it will receive.
    """

    def __init__(self, reference_node: GrammarNode, parent: "CounterNode" = None):
        self.reference_node = reference_node
        self._children = None
        self.parent = parent
        self._is_terminal = True
        self.count = 0
        self.sum_rewards = 0
        self.sum_of_square_rewards = 0
        self.top_reward = -1
        self.top_leaf_node = None  # Only needed to to analyse and debug MCTS
        self.freeze = False  # If true -> prevent any backpropagation to the node's parent

        # A node is said to be solved either if its reference node is a terminal node
        # or if all its children are completely solved.
        # -> this means that the corresponding sub-part of the tree has been already fully visited
        self.solved = False

    def expand(self):
        assert not self.reference_node.is_terminal(), "Try to expand a terminal node"
        self._children = [CounterNode(child_node, parent=self) for child_node in self.reference_node.children()]
        self._is_terminal = False

    def children(self) -> List["CounterNode"]:  # type:ignore
        assert self._children, "Try to access children but the current counter node has not been expanded yet"
        return self._children

    def is_terminal(self) -> bool:
        # For counter node, terminal <=> not having any child
        return self._is_terminal

    def backpropagate(self, new_reward: float, leaf: GrammarNode):
        """
        Given a new_reward update the average reward, sum of square rewards and top reward
        and then pass the information to the parent node
        """
        if self.freeze:
            return

        self.sum_rewards += new_reward
        self.sum_of_square_rewards += new_reward ** 2

        if new_reward > self.top_reward:
            self.top_reward = new_reward
            self.top_leaf_node = leaf

        if self.parent is not None:
            self.parent.backpropagate(new_reward, leaf)

    def set_as_solved(self):
        """
        Set the counter node as solved
        If all his brothers are also solved, back-propagate the information to his parent
        """
        self.solved = True
        if (self.parent is not None) and (not self.parent.freeze):
            brothers = self.parent.children()
            for brother in brothers:
                if not brother.solved:
                    return
            self.parent.set_as_solved()

    def __repr__(self):
        return self.reference_node.__repr__()


class RessourceDistributor:
    """
    Use to select the different allocation strategy for computationnal ressources
    ALL_FROM_ROOT: compute all the tree walks from the tree root
    UNIFORM: the same amount of tree walks will be performed from each depth
    """

    # Other implementation (LINEAR, DYNAMIC, ...) are possible but not detailed here

    def __init__(self, strategy: str, tree_root: GrammarNode, ressources: int):
        self.strategy = strategy if strategy else "ALL_FROM_ROOT"
        self.ressources_to_consume = ressources

        if self.strategy == "ALL_FROM_ROOT":
            self.ressources_to_consume_at_current_depth = self.ressources_to_consume
        elif self.strategy == "UNIFORM":
            average_depth = tree_root.estimate_mean_depth(nb_samples=50)
            # Factor 1.2 in order to consume all the computational ressources before getting to the bottom of the tree
            self.ressources_to_consume_at_current_depth = self.ressources_to_consume / average_depth * 1.2

        self.reset()

    def set_new_position(self, depth):
        self.current_depth = depth
        self.ressources_already_consumed_at_current_depth = 0

    def consume_one_unit(self):
        self.ressources_already_consumed += 1
        self.ressources_already_consumed_at_current_depth += 1

    def still_has_ressources(self):
        return self.ressources_already_consumed < self.ressources_to_consume

    def should_go_down_into_the_tree(self):
        if self.strategy == "ALL_FROM_ROOT":
            return False
        else:
            return self.ressources_already_consumed_at_current_depth >= self.ressources_to_consume_at_current_depth

    def reset(self):
        self.current_depth = 1
        self.ressources_already_consumed = 0
        self.ressources_already_consumed_at_current_depth = 0


class EvalBuffer:
    """
    The evaluation buffer stores the leaves in memory until it is full. 
    Then, the leaves are sent to the LM-based scorer in one single batch. 
    
    Remark : in the following MCTS implementation, it is possible that the 
    random simulations lead to dead-end branches. In such cases, we will, 
    by default, associate a zero reward to those leaves. 
    """

    # This is a vanilla implementation of the evaluation buffer,
    # it does not take advantage of parallelization or memoization
    # but those possible optimizations can be implemented by simply
    # overiding some of the following methods.

    def __init__(self, buffer_size: int, lm_scorer: SentenceScore):
        self.lm_scorer = lm_scorer
        self.buffer_size = buffer_size
        self.buffer: List[Tuple[GrammarNode, CounterNode]] = []
        self.results: List[Tuple[float, GrammarNode, CounterNode]] = []

        self.best_sentence = ""
        self.best_score = -1
        self.score_history = []  # to check score distributions

    def add(self, frontier_counter_node: CounterNode, leaf: GrammarNode):
        if leaf.is_dead_end():  # case of dead-end branches
            self.results.append((0, leaf, frontier_counter_node))
        else:
            self.buffer.append((leaf, frontier_counter_node))
        if len(self.buffer) == self.buffer_size:
            self.force_eval()

    def force_eval(self):
        if len(self.buffer) == 0:
            return []

        scores = self.lm_scorer.compute_score([str(grammar_leaf) for (grammar_leaf, _) in self.buffer])
        self.score_history += scores

        for score, (leaf, frontier_counter_node) in zip(scores, self.buffer):
            self.results.append((score, leaf, frontier_counter_node))
            if score > self.best_score:
                self.best_score = score
                self.best_sentence = str(leaf)

        self.buffer = []

    def pop_results(self):
        output = self.results
        self.results = []
        return output


class MCTS:
    def __init__(
        self,
        lm_scorer: SentenceScore,
        allocation_strategy="ALL_FROM_ROOT",
        buffer_size: int = 1,
        nb_random_restarts=1,
    ):
        self.allocation_strategy = allocation_strategy
        self.nb_random_restarts = nb_random_restarts
        self.eval_buffer = EvalBuffer(buffer_size, lm_scorer)

    def search(self, root: GrammarNode, nb_of_tree_walks: int) -> Tuple[str, float]:
        nb_tree_walks_per_search = nb_of_tree_walks // self.nb_random_restarts
        self.ressource_distributor = RessourceDistributor(
            strategy=self.allocation_strategy, tree_root=root, ressources=nb_tree_walks_per_search
        )

        for i in range(self.nb_random_restarts):
            self.ressource_distributor.reset()
            counter_root = self.single_search(root)

        # we return the counter root of the last search just for debug / analysis purpose 
        return self.eval_buffer.best_sentence, self.eval_buffer.best_score, counter_root

    def single_search(self, root: GrammarNode):
        initial_counter_root = CounterNode(reference_node=root, parent=None)
        counter_root = initial_counter_root
        current_depth = 1

        while (
            not counter_root.reference_node.is_terminal()
            and not counter_root.solved
            and self.ressource_distributor.still_has_ressources()
        ):
            self.ressource_distributor.consume_one_unit()

            # The classic steps of MCTS:
            # 1. selection
            frontier_counter_node = self.selection_phase(counter_root)

            # 2. expansion
            if frontier_counter_node.reference_node.is_terminal():
                # backpropagate the information to the parent in order to avoid selecting this node in the futur
                frontier_counter_node.set_as_solved()
            else:
                frontier_counter_node.expand()

            # 3. simuation
            tmp_node = frontier_counter_node.reference_node
            while not tmp_node.is_terminal():
                tmp_node = tmp_node.random_child()
            random_leaf = tmp_node

            # 4. evaluation
            # contrary to vanilla MCTS, we use a buffer system for evaluation
            self.eval_buffer.add(frontier_counter_node, random_leaf)
            results = self.eval_buffer.pop_results()
            # most of the time, results is an empty list because the buffer is only evaluated when it is full

            # 5. backpropagation (every buffer_size nb of steps)
            for reward, leaf, frontier_counter_node in results:
                frontier_counter_node.backpropagate(reward, leaf)

            # After each iteration, we query the ressource distributor to know if we should continue
            # to perform the tree walks from current root or if we should go down to the best child
            if self.ressource_distributor.should_go_down_into_the_tree():
                # Force the evaluation of the leaves that still remain in the buffer
                self.eval_buffer.force_eval()
                for reward, leaf, frontier_counter_node in self.eval_buffer.pop_results():
                    frontier_counter_node.backpropagate(reward, leaf)

                # Freeze current_root to avoid modifying the counter in futur backprops
                counter_root.freeze = True

                # Go to the best child (other strategies could be implemented here)
                counter_root = max(counter_root.children(), key=lambda child: child.top_reward)
                current_depth += 1
                self.ressource_distributor.set_new_position(current_depth)

        self.eval_buffer.force_eval()
        return initial_counter_root # just for debug 

    ### SELECTION PHASE ###
    # Go down to the frontier of the visited tree
    # by sucessively visiting the child that maximises the single player UCB

    def selection_phase(self, root: CounterNode) -> CounterNode:
        node = root
        node.count += 1
        while not node.is_terminal():
            node = self.selection_policy(node)
            node.count += 1
        return node

    def selection_policy(self, counter_node: CounterNode) -> CounterNode:
        # if one child of current node has not been visited yet : visit it first
        for child in counter_node.children():
            if child.count == 0:
                return child
        # else select the node that maximise the UCB among the nodes that have not been solved yet
        unsolved_children = [children for children in counter_node.children() if not children.solved]
        return max(unsolved_children, key=lambda node: self.single_player_ucb(node, counter_node))

    @staticmethod
    def single_player_ucb(child: CounterNode, parent: CounterNode, c=1, d=100) -> float:
        """
        Compute UCB for single-player context as proposed by
        Schadda, Winandsan, Taka, Uiterwijka. "Single-Player Monte-Carlo Tree Search for SameGame"
        """
        return (
            child.sum_rewards / child.count
            + math.sqrt(c * math.log(parent.count / child.count))
            + math.sqrt(
                (child.sum_of_square_rewards - child.count * ((child.sum_rewards / child.count) ** 2) + d)
                / child.count
            )
        )

