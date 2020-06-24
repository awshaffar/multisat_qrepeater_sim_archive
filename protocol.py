import sys
import abc
from abc import abstractmethod

if sys.version_info >= (3, 4):
    ABC = abc.ABC
else:
    ABC = abc.ABCMeta('ABC', (), {})

class Protocol(ABC):
    def __init__(self, world):
        self.world = world


    @abstractmethod
    def setup(self):
        """Setup function to be called after the world has been initialized.

        Should analyze the world to see if the protocol is applicable to the
        situation and possibly label stations/sources so they are easy
        to access in the check method of the protocol.
        """

    @abstractmethod
    def check(self):
        """The main method of the protocol.

        Should analyze the current status of the world and event_queue to
        make decisions about next steps.
        """
        pass
